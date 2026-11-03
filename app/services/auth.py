"""Имэйл + нууц үгээр нэвтрэх. Нууц үгийг PBKDF2-SHA256-аар хадгалж, JWT токен олгоно."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import select

from app.config import settings
from app.db import User, session_scope
from app.models.schemas import UserResponse

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 200_000
_ephemeral_secret: Optional[str] = None


def _secret() -> str:
    global _ephemeral_secret
    if settings.jwt_secret:
        return settings.jwt_secret
    if _ephemeral_secret is None:
        logger.warning("JWT_SECRET тохируулаагүй — түр нууц ашиглаж байна, сервер дахин эхлэхэд бүх хэрэглэгч гарна")
        _ephemeral_secret = secrets.token_urlsafe(48)
    return _ephemeral_secret


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def create_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + settings.jwt_ttl_days * 86400}, _secret(), "HS256")


def decode_token(token: str) -> Optional[str]:
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        return None


# ---------- Хэрэглэгч ----------

def register(email: str, password: str) -> UserResponse:
    email = email.strip().lower()
    with session_scope() as s:
        if s.scalar(select(User).where(User.email == email)):
            raise HTTPException(status_code=409, detail="EMAIL_TAKEN: Энэ имэйлээр бүртгэл үүссэн байна")
        user = User(email=email, password_hash=hash_password(password))
        s.add(user)
        s.flush()
        return UserResponse(id=user.id, email=user.email)


def authenticate(email: str, password: str) -> Optional[UserResponse]:
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == email.strip().lower()))
        if user and verify_password(password, user.password_hash):
            return UserResponse(id=user.id, email=user.email)
    return None


def user_from_token(token: Optional[str]) -> Optional[UserResponse]:
    user_id = decode_token(token) if token else None
    if not user_id:
        return None
    with session_scope() as s:
        user = s.get(User, user_id)
        return UserResponse(id=user.id, email=user.email) if user else None


def token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # <audio src>, DOCX татах линк зэрэг header явуулж чаддаггүй хүсэлтэд
    return request.query_params.get("token")


def current_user(request: Request) -> UserResponse:
    """FastAPI dependency — нэвтрээгүй бол 401."""
    user = user_from_token(token_from_request(request))
    if not user:
        raise HTTPException(
            status_code=401,
            detail="UNAUTHORIZED: Нэвтэрнэ үү",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
