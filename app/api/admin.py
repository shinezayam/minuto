"""Дотоод админ хуудас: Enterprise үнийн санал авах хүсэлтүүд. Админ = ADMIN_EMAILS-д байгаа и-мэйл."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.config import settings
from app.db import ContactRequest, session_scope
from app.models.schemas import UserResponse
from app.services.auth import current_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

Status = Literal["new", "contacted", "closed"]


def is_admin(user: UserResponse) -> bool:
    admins = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    return bool(admins) and user.email.strip().lower() in admins


def require_admin(user: UserResponse = Depends(current_user)) -> UserResponse:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="FORBIDDEN: Энэ хуудсыг зөвхөн админ харна")
    return user


class StatusIn(BaseModel):
    status: Status


def _dict(r: ContactRequest) -> dict:
    created = r.created_at
    return {"id": r.id, "name": r.name, "email": r.email, "company": r.company, "phone": r.phone,
            "message": r.message, "status": r.status, "created_at": created.isoformat() if created else None}


@router.get("/me")
def admin_me(user: UserResponse = Depends(current_user)) -> dict:
    return {"is_admin": is_admin(user)}


@router.get("/contact-requests")
def list_contact_requests(status: Optional[Status] = Query(default=None), _: UserResponse = Depends(require_admin)) -> dict:
    with session_scope() as s:
        q = select(ContactRequest).order_by(ContactRequest.created_at.desc()).limit(200)
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
