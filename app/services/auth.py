"""Имэйл + нууц үгээр нэвтрэх. Нууц үгийг PBKDF2-SHA256-аар хадгалж, JWT токен олгоно."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
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


def _token_version(user_id: str) -> int:
    with session_scope() as s:
        user = s.get(User, user_id)
        return int(user.token_version or 0) if user else 0


def create_token(user_id: str) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "ver": _token_version(user_id), "iat": now, "exp": now + settings.jwt_ttl_days * 86400}
    return jwt.encode(payload, _secret(), "HS256")


def _decode(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def decode_token(token: str) -> Optional[str]:
    """Нэвтрэлтийн (access) токеныг задалж user id буцаана. Тасалбар (ticket) энд хүчингүй."""
    payload = _decode(token)
    if not payload or payload.get("typ") == "ticket":
        return None
    return payload.get("sub")


# ---------- Богино настай, цар хүрээтэй тасалбар (WebSocket, <audio>) ----------
# URL-д урт настай нэвтрэлтийн токен тавихгүйн тулд: нэг хурал + нэг зорилгод зориулсан, хэдэн секундын настай.
_used_tickets: dict[str, float] = {}
_used_lock = threading.Lock()


def create_ticket(user_id: str, meeting_id: str, scope: str, ttl_sec: int) -> str:
    now = int(time.time())
    payload = {
        "typ": "ticket", "scope": scope, "sub": user_id, "mid": meeting_id,
        "ver": _token_version(user_id), "jti": secrets.token_urlsafe(12), "iat": now, "exp": now + ttl_sec,
    }
    return jwt.encode(payload, _secret(), "HS256")


def verify_ticket(ticket: Optional[str], scope: str, meeting_id: str, single_use: bool = False) -> Optional[UserResponse]:
    payload = _decode(ticket) if ticket else None
    if not payload or payload.get("typ") != "ticket" or payload.get("scope") != scope or payload.get("mid") != meeting_id:
        return None
    user = _user_if_current(payload)
    if not user:
        return None
    if single_use:
        now = time.time()
        with _used_lock:
            for k in [k for k, exp in _used_tickets.items() if exp < now]:
                del _used_tickets[k]
            if payload["jti"] in _used_tickets:
                return None  # дахин ашиглах оролдлого
            _used_tickets[payload["jti"]] = float(payload["exp"])
    return user


def _user_if_current(payload: dict) -> Optional[UserResponse]:
    """Токен/тасалбарт заасан хэрэглэгч байгаа бөгөөд token_version нь таарч байвал."""
    user_id = payload.get("sub")
    if not user_id:
        return None
    with session_scope() as s:
        user = s.get(User, user_id)
        if not user or int(payload.get("ver", 0)) != int(user.token_version or 0):
            return None
        return _user_response(user)


def revoke_all_tokens(user_id: str) -> None:
    """Бүх төхөөрөмж дээрх токенууд, тасалбарууд хүчингүй болно."""
    with session_scope() as s:
        user = s.get(User, user_id)
        if user:
            user.token_version = int(user.token_version or 0) + 1


# ---------- Хэрэглэгч ----------

def _user_response(user: User) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, name=user.name)


def register(email: str, password: str, name: Optional[str] = None) -> UserResponse:
    email = email.strip().lower()
    with session_scope() as s:
        if s.scalar(select(User).where(User.email == email)):
            raise HTTPException(status_code=409, detail="EMAIL_TAKEN: Энэ имэйлээр бүртгэл үүссэн байна")
        user = User(email=email, password_hash=hash_password(password), name=(name or "").strip() or None)
        s.add(user)
        s.flush()
        return _user_response(user)


def authenticate(email: str, password: str) -> Optional[UserResponse]:
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == email.strip().lower()))
        if user and verify_password(password, user.password_hash):
            return _user_response(user)
    return None


def user_from_token(token: Optional[str]) -> Optional[UserResponse]:
    payload = _decode(token) if token else None
    if not payload or payload.get("typ") == "ticket":
        return None
    return _user_if_current(payload)


def update_name(user_id: str, name: str) -> Optional[UserResponse]:
    with session_scope() as s:
        user = s.get(User, user_id)
        if not user:
            return None
        user.name = name.strip() or None
        return _user_response(user)


# Хуучин клиентүүд <audio src>, татах линкэнд ?token= тавьдаг байсан. Зөвхөн эдгээр route-д, шилжилтийн хугацаанд л зөвшөөрнө.
_LEGACY_QUERY_TOKEN_PATHS = re.compile(r"^/api/meetings/[^/]+/(audio|export\.docx|export\.pdf)$")


def token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    if settings.allow_query_token and _LEGACY_QUERY_TOKEN_PATHS.match(request.url.path):
        token = request.query_params.get("token")
        if token:
            logger.warning("Deprecated ?token= used on %s", request.url.path)
        return token
    return None


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
