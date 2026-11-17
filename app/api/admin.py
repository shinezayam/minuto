"""Дотоод админ хуудас: Enterprise үнийн санал авах хүсэлтүүд. Админ = ADMIN_EMAILS-д байгаа и-мэйл."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import ContactRequest, session_scope
from app.models.schemas import UserResponse
from app.services import admins
from app.services.auth import current_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

Status = Literal["new", "contacted", "closed"]
Kind = Literal["enterprise", "feedback", "all"]


def is_admin(user: UserResponse) -> bool:
    return admins.is_admin(user.email)


def require_admin(user: UserResponse = Depends(current_user)) -> UserResponse:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="FORBIDDEN: Энэ хуудсыг зөвхөн админ харна")
    return user


def require_owner(user: UserResponse = Depends(current_user)) -> UserResponse:
    """Үндсэн админ (Render-ийн ADMIN_EMAILS) — зөвхөн тэд админ нэмж, хасна."""
    if not admins.is_owner(user.email):
        raise HTTPException(status_code=403, detail="FORBIDDEN: Админ нэмэх, хасах эрх зөвхөн үндсэн админд бий")
    return user


class AdminIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class StatusIn(BaseModel):
    status: Status


def _dict(r: ContactRequest) -> dict:
    created = r.created_at
    return {"id": r.id, "name": r.name, "email": r.email, "company": r.company, "phone": r.phone,
            "message": r.message, "status": r.status, "kind": r.kind or "enterprise", "category": r.category,
            "created_at": created.isoformat() if created else None}


@router.get("/me")
def admin_me(user: UserResponse = Depends(current_user)) -> dict:
    return {"is_admin": is_admin(user), "is_owner": admins.is_owner(user.email)}


@router.get("/admins")
def list_admins(_: UserResponse = Depends(require_owner)) -> dict:
    return {"owners": admins.owner_emails(), "admins": admins.list_grants()}


@router.post("/admins", status_code=201)
def add_admin(body: AdminIn, user: UserResponse = Depends(require_owner)) -> dict:
    try:
        return admins.grant(body.email, user.email)
    except admins.AdminError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.delete("/admins/{email}", status_code=204, response_model=None)
def remove_admin(email: str, _: UserResponse = Depends(require_owner)) -> None:
    try:
        admins.revoke(email)
    except admins.AdminError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.get("/contact-requests")
def list_contact_requests(status: Optional[Status] = Query(default=None), kind: Kind = Query(default="enterprise"),
                          _: UserResponse = Depends(require_admin)) -> dict:
    with session_scope() as s:
        q = select(ContactRequest).order_by(ContactRequest.created_at.desc()).limit(200)
        if kind != "all":
            q = q.where(ContactRequest.kind == kind)
        if status:
            q = q.where(ContactRequest.status == status)
        return {"requests": [_dict(r) for r in s.scalars(q).all()]}


@router.patch("/contact-requests/{request_id}")
def update_contact_request(request_id: str, body: StatusIn, _: UserResponse = Depends(require_admin)) -> dict:
    with session_scope() as s:
        r = s.get(ContactRequest, request_id)
        if r is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND: Хүсэлт олдсонгүй")
        r.status = body.status
        s.flush()
        return _dict(r)
