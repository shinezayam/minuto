"""Хурлын хандалтын ЦОРЫН ГАНЦ эх үүсвэр: хэн, ямар хурал дээр, ямар үйлдэл хийж болох вэ.

Хувийн хурал (workspace_id = NULL): зөвхөн эзэн (өмнөх үйл ажиллагаатай яг ижил).
Workspace-ийн хурал: гишүүний үүргээр (role) шийднэ:

  Үйлдэл                              owner  admin  editor        viewer
  харах (хурал, бичвэр, аудио, export)  ✓      ✓      ✓             ✓
  хурал үүсгэх                          ✓      ✓      ✓             –
  засах (бичвэр, тэмдэглэл, боловсруулах) ✓      ✓      ✓             –
  устгах / архивлах                     ✓      ✓      өөрийнхөө     –

Гишүүн биш хүн хурлыг огт олж харахгүй (404 — оршин байгааг ч мэдэгдэхгүй). Гишүүн боловч эрхгүй бол 403.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from app.db import Meeting, WorkspaceMember

ACTIONS = ("view", "create", "edit", "delete")

# action → үүргүүд (delete дээр editor нь зөвхөн өөрийн хурлыг устгана — decide() дотор тусгай шалгана)
_ALLOWED = {
    "view": {"owner", "admin", "editor", "viewer"},
    "create": {"owner", "admin", "editor"},
    "edit": {"owner", "admin", "editor"},
    "delete": {"owner", "admin", "editor"},
}


class AccessDenied(Exception):
    """Хурлыг харж болох боловч энэ үйлдэл хийх эрхгүй (403)."""


def workspace_role(session, user_id: str, workspace_id: str) -> Optional[str]:
    return session.scalar(
        select(WorkspaceMember.role).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
    )


def decide(session, user_id: str, meeting: Meeting, action: str) -> str:
    """"ok" | "forbidden" (харж болно, үйлдэл хийж болохгүй) | "none" (огт хандах эрхгүй)."""
    if action not in ACTIONS:
        raise ValueError(f"Тодорхойгүй action: {action}")
    if meeting.workspace_id is None:
        return "ok" if meeting.owner_id == user_id else "none"
    role = workspace_role(session, user_id, meeting.workspace_id)
    if role is None:
        return "none"
    if role not in _ALLOWED[action]:
        return "forbidden"
    if action == "delete" and role == "editor" and meeting.owner_id != user_id:
        return "forbidden"
    return "ok"


def role_for(session, user_id: str, meeting: Meeting) -> Optional[str]:
    """Хэрэглэгчийн энэ хурал дээрх үүрэг (frontend-д товчлуур нуух/харуулахад)."""
    if meeting.workspace_id is None:
        return "owner" if meeting.owner_id == user_id else None
    return workspace_role(session, user_id, meeting.workspace_id)


def accessible_workspace_ids(session, user_id: str, min_action: str = "view") -> list[str]:
    allowed = _ALLOWED[min_action]
    return list(
        session.scalars(
            select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id, WorkspaceMember.role.in_(allowed))
        )
    )
