from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.models.schemas import AuthRequest, AuthResponse, UserResponse, UserUpdate
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: AuthRequest) -> AuthResponse:
    if not settings.allow_registration:
        raise HTTPException(status_code=403, detail="REGISTRATION_CLOSED: Бүртгэл хаалттай байна")
    user = auth.register(body.email, body.password, body.name)
    return AuthResponse(token=auth.create_token(user.id), user=user)


@router.post("/login", response_model=AuthResponse)
def login(body: AuthRequest) -> AuthResponse:
    user = auth.authenticate(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS: Имэйл эсвэл нууц үг буруу байна")
    return AuthResponse(token=auth.create_token(user.id), user=user)


class GoogleAuthIn(BaseModel):
    credential: str = Field(min_length=20, max_length=4096)


@router.get("/google/config")
def google_config() -> dict:
    """Frontend "Google-ээр нэвтрэх" товчийг харуулах эсэх (client ID нь нууц биш)."""
    ids = auth.google_client_ids()
    return {"enabled": bool(ids), "client_id": ids[0] if ids else None}


@router.post("/google", response_model=AuthResponse)
def google_sign_in(body: GoogleAuthIn, response: Response) -> AuthResponse:
    """Google Identity Services-ийн ID token (credential)-оор нэвтрэх/бүртгүүлэх. Шинэ хэрэглэгч бол 201."""
    try:
        user, created = auth.google_sign_in(body.credential)
    except auth.GoogleAuthError as e:
        status = {"GOOGLE_DISABLED": 404, "GOOGLE_UNAVAILABLE": 503, "REGISTRATION_CLOSED": 403,
                  "GOOGLE_ACCOUNT_MISMATCH": 409}.get(e.code, 401)
        raise HTTPException(status_code=status, detail=f"{e.code}: {e.message}") from None
    if created:
        response.status_code = 201
    return AuthResponse(token=auth.create_token(user.id), user=user)


@router.get("/me", response_model=UserResponse)
def me(user: UserResponse = Depends(auth.current_user)) -> UserResponse:
    return user


@router.post("/logout-all")
def logout_all(user: UserResponse = Depends(auth.current_user)) -> dict:
    """Бүх төхөөрөмж дээрх токенийг хүчингүй болгоно; энэ төхөөрөмжид шинэ токен өгнө."""
    auth.revoke_all_tokens(user.id)
    return {"token": auth.create_token(user.id)}


@router.patch("/me", response_model=UserResponse)
def update_me(body: UserUpdate, user: UserResponse = Depends(auth.current_user)) -> UserResponse:
    updated = auth.update_name(user.id, body.name)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return updated
