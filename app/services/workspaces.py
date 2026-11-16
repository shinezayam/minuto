"""Workspace service - багийн хамтын ажиллагаа."""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import delete, select, update

from app.db import Meeting, Project, User, Workspace, WorkspaceMember, session_scope

logger = logging.getLogger(__name__)


def ensure_personal_workspace(user_id: str) -> str:
    """Хэрэглэгчийн хувийн workspace байхгүй бол үүсгэнэ."""
    with session_scope() as s:
        # Хувийн workspace хайх
        ws = s.scalars(
            select(Workspace).where(
                Workspace.owner_id == user_id,
                Workspace.workspace_type == "personal"
            )
        ).first()
        
        if ws:
            return ws.id
        
        # Шинэ хувийн workspace үүсгэх
        user = s.get(User, user_id)
        ws = Workspace(
            name=f"{user.email}-н хувийн",
            workspace_type="personal",
            owner_id=user_id,
        )
        s.add(ws)
        s.flush()
        
        # Owner гишүүн нэмэх
        member = WorkspaceMember(
            workspace_id=ws.id,
            user_id=user_id,
            role="owner",
        )
        s.add(member)
        logger.info("Created personal workspace for user %s", user_id)
        return ws.id


def list_user_workspaces(user_id: str) -> List[dict]:
    """Хэрэглэгчийн бүх workspace-уудыг жагсаах."""
    ensure_personal_workspace(user_id)
    
    with session_scope() as s:
        # Хэрэглэгчийн гишүүнчлэлтэй workspace-ууд
        rows = s.scalars(
            select(Workspace)
            .join(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id)
            .where(WorkspaceMember.user_id == user_id)
            .order_by(Workspace.workspace_type, Workspace.name)
        ).all()
        
        return [
            {
                "id": ws.id,
                "name": ws.name,
                "description": ws.description,
                "workspace_type": ws.workspace_type,
                "owner_id": ws.owner_id,
                "role": s.scalars(
                    select(WorkspaceMember.role).where(
                        WorkspaceMember.workspace_id == ws.id,
                        WorkspaceMember.user_id == user_id
                    )
                ).first(),
            }
            for ws in rows
        ]


def get_workspace(workspace_id: str, user_id: str) -> Optional[dict]:
    """Workspace-г авах - хэрэглэгч гишүүн эсэхийг шалгана."""
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        if not ws:
            return None
        
        # Гишүүн эсэхийг шалгах
        member = s.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id
            )
        ).first()
        
        if not member:
            return None
        
        return {
            "id": ws.id,
            "name": ws.name,
            "description": ws.description,
            "workspace_type": ws.workspace_type,
            "owner_id": ws.owner_id,
            "role": member.role,
        }


def create_organization_workspace(user_id: str, name: str, description: str = None) -> dict:
    """Шинэ organization workspace үүсгэх."""
    with session_scope() as s:
        ws = Workspace(
            name=name,
            description=description,
            workspace_type="organization",
            owner_id=user_id,
        )
        s.add(ws)
        s.flush()
        
        # Owner гишүүн нэмэх
        member = WorkspaceMember(
            workspace_id=ws.id,
            user_id=user_id,
            role="owner",
        )
        s.add(member)
        logger.info("Created organization workspace '%s' by user %s", name, user_id)
        
        return {
            "id": ws.id,
            "name": ws.name,
            "description": ws.description,
            "workspace_type": ws.workspace_type,
            "owner_id": ws.owner_id,
            "role": "owner",
        }


class WorkspaceError(Exception):
    """Workspace-ийн бизнес дүрмийн зөрчил: code (тогтмол), HTTP status, хэрэглэгчид харуулах мессеж."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.message}"


_RANK = {"owner": 4, "admin": 3, "editor": 2, "viewer": 1}
# Хэн ямар үүрэг ОЛГОХ / ТАСЛАХ / ХАСАХ эрхтэй (нэг дүрэм, гурван үйлдэлд):
#   owner → admin, editor, viewer-тэй хэн ч    admin → зөвхөн editor, viewer-тэй    editor/viewer → хэнд ч үгүй
_MANAGEABLE = {"owner": {"admin", "editor", "viewer"}, "admin": {"editor", "viewer"}}


def _audit(workspace_id: str, actor_id: str, action: str, target: str, detail: str = "") -> None:
    logger.info("AUDIT workspace=%s actor=%s action=%s target=%s %s", workspace_id, actor_id, action, target, detail)


def _member_role(s, workspace_id: str, user_id: str) -> Optional[str]:
    return s.scalars(
        select(WorkspaceMember.role).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id)
    ).first()


def _require_member(s, workspace_id: str, actor_id: str) -> tuple:
    """Workspace + actor-ийн үүрэг. Гишүүн биш бол оршин байгааг ч мэдэгдэхгүй (404)."""
    ws = s.get(Workspace, workspace_id)
    role = _member_role(s, workspace_id, actor_id) if ws else None
    if ws is None or role is None:
        raise WorkspaceError("WORKSPACE_NOT_FOUND", 404, "Workspace олдсонгүй")
    return ws, role


def invite_member(workspace_id: str, inviter_id: str, email: str, role: str = "viewer") -> dict:
    """Гишүүн нэмнэ. owner admin/editor/viewer-ээр, admin зөвхөн editor/viewer-ээр урина. Зөвхөн байгаа хэрэглэгчийг (accept алхамгүй) — AUDIT лог бичигдэнэ."""
    with session_scope() as s:
        ws, inviter_role = _require_member(s, workspace_id, inviter_id)
        if ws.workspace_type == "personal":
            raise WorkspaceError("PERSONAL_WORKSPACE", 422, "Хувийн workspace-д гишүүн урих боломжгүй")
        if role not in _MANAGEABLE.get(inviter_role, set()):
            raise WorkspaceError("FORBIDDEN_ROLE", 403, "Та энэ үүргээр гишүүн урих эрхгүй")
        user = s.scalars(select(User).where(User.email == email.strip().lower())).first()
        if not user:
            # Ямар имэйл бүртгэлтэйг задруулахгүйн тулд ерөнхий мессеж
            raise WorkspaceError("INVITE_FAILED", 400, "Урих боломжгүй байна. Имэйлийг шалгана уу")
        if _member_role(s, workspace_id, user.id):
            raise WorkspaceError("ALREADY_MEMBER", 409, "Энэ хэрэглэгч аль хэдийн гишүүн байна")
        s.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role))
        _audit(workspace_id, inviter_id, "invite", user.id, f"role={role}")
        return {"user_id": user.id, "email": user.email, "name": user.name, "role": role}


def list_members(workspace_id: str, user_id: str) -> List[dict]:
    """Workspace-ийн гишүүдийг жагсаах."""
    with session_scope() as s:
        # Хэрэглэгч гишүүн эсэхийг шалгах
        is_member = s.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id
            )
        ).first()
        
        if not is_member:
            return []
        
        # Хоёр entity (гишүүн + хэрэглэгч) сонгож байгаа тул scalars() биш execute(): өмнө нь "cannot unpack" гэж 500 өгдөг байсан
        rows = s.execute(
            select(WorkspaceMember, User)
            .join(User, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id == workspace_id)
        ).all()
        
        return [
            {
                "user_id": member.user_id,
                "email": user.email,
                "name": user.name,
                "role": member.role,
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            }
            for member, user in rows
        ]


def remove_member(workspace_id: str, remover_id: str, user_id: str) -> None:
    """Гишүүн хасна. Өөрөө гарч болно (owner-оос бусад). owner admin/editor/viewer-ийг, admin зөвхөн editor/viewer-ийг хасна; owner-ийг хэн ч хасахгүй."""
    with session_scope() as s:
        ws, remover_role = _require_member(s, workspace_id, remover_id)
        target_role = _member_role(s, workspace_id, user_id)
        if target_role is None:
            raise WorkspaceError("MEMBER_NOT_FOUND", 404, "Гишүүн олдсонгүй")
        if target_role == "owner" or ws.owner_id == user_id:
            raise WorkspaceError("CANNOT_REMOVE_OWNER", 403, "Эзэмшигчийг хасах боломжгүй (эхлээд эзэмшлийг шилжүүлнэ үү)")
        if user_id != remover_id and target_role not in _MANAGEABLE.get(remover_role, set()):
            raise WorkspaceError("FORBIDDEN_ROLE", 403, "Та энэ гишүүнийг хасах эрхгүй")
        s.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id))
        _audit(workspace_id, remover_id, "remove" if user_id != remover_id else "leave", user_id, f"role={target_role}")


def change_role(workspace_id: str, actor_id: str, user_id: str, new_role: str) -> dict:
    """Үүрэг солино: owner → admin/editor/viewer; admin → зөвхөн editor/viewer (admin-ийг өөрчлөх, дэвшүүлэх боломжгүй)."""
    with session_scope() as s:
        ws, actor_role = _require_member(s, workspace_id, actor_id)
        target_role = _member_role(s, workspace_id, user_id)
        if target_role is None:
            raise WorkspaceError("MEMBER_NOT_FOUND", 404, "Гишүүн олдсонгүй")
        if target_role == "owner":
            raise WorkspaceError("CANNOT_CHANGE_OWNER", 403, "Эзэмшигчийн үүргийг солих боломжгүй (эзэмшлийг шилжүүлнэ үү)")
        manageable = _MANAGEABLE.get(actor_role, set())
        if target_role not in manageable or new_role not in manageable:
            raise WorkspaceError("FORBIDDEN_ROLE", 403, "Та энэ үүрэг оноох/өөрчлөх эрхгүй")
        s.execute(
            update(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id).values(role=new_role)
        )
        _audit(workspace_id, actor_id, "change_role", user_id, f"{target_role}->{new_role}")
        user = s.get(User, user_id)
        return {"user_id": user_id, "email": user.email, "name": user.name, "role": new_role}


def delete_workspace(workspace_id: str, actor_id: str) -> None:
    """Зөвхөн owner. Байгууллагын workspace-ийг устгана; доторх хурлууд устахгүй — үүсгэсэн хүндээ хувийн хурал болж үлдэнэ."""
    with session_scope() as s:
        ws, role = _require_member(s, workspace_id, actor_id)
        if role != "owner":
            raise WorkspaceError("FORBIDDEN_ROLE", 403, "Зөвхөн эзэмшигч workspace-ийг устгана")
        if ws.workspace_type == "personal":
            raise WorkspaceError("PERSONAL_WORKSPACE", 422, "Хувийн workspace-ийг устгах боломжгүй")
        # DB дээр FK SET NULL нь SQLite-д автоматаар ажиллахгүй байж болох тул тодорхой хийнэ
        s.execute(update(Meeting).where(Meeting.workspace_id == workspace_id).values(workspace_id=None, project_id=None))
        s.execute(delete(Project).where(Project.workspace_id == workspace_id))
        s.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id))
        s.delete(ws)
        _audit(workspace_id, actor_id, "delete_workspace", workspace_id)


def transfer_ownership(workspace_id: str, actor_id: str, new_owner_id: str) -> None:
    """Зөвхөн owner. Шинэ эзэн нь одоогийн гишүүн байх ёстой; хуучин эзэн admin болно."""
    with session_scope() as s:
        ws, role = _require_member(s, workspace_id, actor_id)
        if role != "owner":
            raise WorkspaceError("FORBIDDEN_ROLE", 403, "Зөвхөн эзэмшигч эзэмшлийг шилжүүлнэ")
        if ws.workspace_type == "personal":
            raise WorkspaceError("PERSONAL_WORKSPACE", 422, "Хувийн workspace-ийн эзэмшлийг шилжүүлэх боломжгүй")
        if new_owner_id == actor_id or _member_role(s, workspace_id, new_owner_id) is None:
            raise WorkspaceError("INVALID_NEW_OWNER", 422, "Шинэ эзэмшигч нь одоогийн өөр гишүүн байх ёстой")
        s.execute(update(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == new_owner_id).values(role="owner"))
        s.execute(update(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == actor_id).values(role="admin"))
        ws.owner_id = new_owner_id
        _audit(workspace_id, actor_id, "transfer_ownership", new_owner_id)


def check_permission(workspace_id: str, user_id: str, required_role: str = "viewer") -> bool:
    """Хэрэглэгчийн эрх шалгах."""
    roles = {"owner": 4, "admin": 3, "editor": 2, "viewer": 1}
    required_level = roles.get(required_role, 0)
    
    with session_scope() as s:
        member = s.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id
            )
        ).first()
        
        if not member:
            return False
        
        user_level = roles.get(member.role, 0)
        return user_level >= required_level


# Project functions

def list_projects(workspace_id: str, user_id: str) -> List[dict]:
    """Workspace-ийн project-уудыг жагсаах."""
    if not check_permission(workspace_id, user_id, "viewer"):
        return []
    
    with session_scope() as s:
        rows = s.scalars(
            select(Project)
            .where(Project.workspace_id == workspace_id)
            .order_by(Project.name)
        ).all()
        
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "color": p.color,
                "workspace_id": p.workspace_id,
            }
            for p in rows
        ]


def create_project(workspace_id: str, user_id: str, name: str, description: str = None, color: str = "#5965D8") -> Optional[dict]:
    """Project үүсгэх."""
    if not check_permission(workspace_id, user_id, "admin"):
        return None
    
    with session_scope() as s:
        project = Project(
            workspace_id=workspace_id,
            name=name,
            description=description,
            color=color,
        )
        s.add(project)
        s.flush()
        
        return {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "color": project.color,
            "workspace_id": project.workspace_id,
        }


def delete_project(project_id: str, user_id: str, workspace_id: Optional[str] = None) -> bool:
    """Project устгах (workspace_id өгвөл тэр workspace-ийнх мөн эсэхийг шалгана). Доторх хурлууд устахгүй, project-гүй болно."""
    with session_scope() as s:
        project = s.get(Project, project_id)
        if not project or (workspace_id is not None and project.workspace_id != workspace_id):
            return False

        if not check_permission(project.workspace_id, user_id, "admin"):
            return False

        s.execute(update(Meeting).where(Meeting.project_id == project_id).values(project_id=None))
        s.delete(project)
        return True
