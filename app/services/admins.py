"""Админы эрх.

- Үндсэн админ (owner): Render env ADMIN_EMAILS-д байгаа и-мэйл. Зөвхөн тэд бусад админыг нэмж, хасна.
- Нэмэгдсэн админ: admin_grants хүснэгтэд байгаа и-мэйл (үндсэн админ сайтаас нэмнэ). Хүсэлтүүд, Sandbox зэргийг ашиглана,
  гэхдээ өөр админ нэмж, хасах эрхгүй.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select

from app.config import settings
from app.db import AdminGrant, session_scope

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AdminError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def owner_emails() -> List[str]:
    return sorted({_norm(e) for e in settings.admin_emails.split(",") if _norm(e)})


def is_owner(email: Optional[str]) -> bool:
    e = _norm(email)
    return bool(e) and e in owner_emails()


def is_admin(email: Optional[str]) -> bool:
    e = _norm(email)
    if not e:
        return False
    if e in owner_emails():
        return True
    with session_scope() as s:
        return s.get(AdminGrant, e) is not None


def list_grants() -> List[dict]:
    with session_scope() as s:
        rows = s.scalars(select(AdminGrant).order_by(AdminGrant.created_at.asc())).all()
        return [{"email": r.email, "added_by": r.added_by,
                 "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


def grant(email: str, by: str) -> dict:
    e = _norm(email)
    if not _EMAIL.match(e) or len(e) > 320:
        raise AdminError(422, "INVALID_EMAIL: И-мэйл хаяг буруу байна")
    if e in owner_emails():
        raise AdminError(409, "ALREADY_OWNER: Энэ хүн үндсэн админ байна")
    with session_scope() as s:
        if s.get(AdminGrant, e) is not None:
            raise AdminError(409, "ALREADY_ADMIN: Энэ хүн аль хэдийн админ байна")
        s.add(AdminGrant(email=e, added_by=_norm(by), created_at=datetime.now(timezone.utc)))
    return {"email": e, "added_by": _norm(by)}


def revoke(email: str) -> None:
    e = _norm(email)
    if e in owner_emails():
        raise AdminError(409, "OWNER: Үндсэн админыг зөвхөн Render-ийн ADMIN_EMAILS-ээс хасна")
    with session_scope() as s:
        row = s.get(AdminGrant, e)
        if row is None:
            raise AdminError(404, "NOT_FOUND: Ийм админ алга")
        s.delete(row)
