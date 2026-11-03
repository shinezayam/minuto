from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.models.schemas import AuthRequest, AuthResponse, UserResponse
from app.services import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: AuthRequest) -> AuthResponse:
    if not settings.allow_registration:
        raise HTTPException(status_code=403, detail="REGISTRATION_CLOSED: Бүртгэл хаалттай байна")
    user = auth.register(body.email, body.password)
    return AuthResponse(token=auth.create_token(user.id), user=user)


@router.post("/login", response_model=AuthResponse)
def login(body: AuthRequest) -> AuthResponse:
    user = auth.authenticate(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS: Имэйл эсвэл нууц үг буруу байна")
    return AuthResponse(token=auth.create_token(user.id), user=user)


@router.get("/me", response_model=UserResponse)
def me(user: UserResponse = Depends(auth.current_user)) -> UserResponse:
    return user
