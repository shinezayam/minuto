"""Workspace service - багийн хамтын ажиллагаа."""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import select, delete

from app.db import Workspace, WorkspaceMember, Project, User, session_scope

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


def invite_member(workspace_id: str, inviter_id: str, email: str, role: str = "viewer") -> Optional[dict]:
    """Гишүүн урих - email-р хэрэглэгч олно."""
    with session_scope() as s:
        # Уригч нь owner эсвэл admin эсэхийг шалгах
        inviter_role = s.scalars(
            select(WorkspaceMember.role).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == inviter_id
            )
        ).first()
        
        if inviter_role not in ("owner", "admin"):
            return None
        
        # Хэрэглэгч олох
        user = s.scalars(select(User).where(User.email == email)).first()
        if not user:
            return None
        
        # Аль хэдийн гишүүн эсэхийг шалгах
        existing = s.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user.id
            )
        ).first()
        
        if existing:
            return None
        
        # Гишүүн нэмэх
        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user.id,
            role=role,
        )
        s.add(member)
        logger.info("Invited %s to workspace %s as %s", email, workspace_id, role)
        
        return {
            "user_id": user.id,
            "email": user.email,
            "name": user.name,
            "role": role,
        }


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
        
        rows = s.scalars(
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


def remove_member(workspace_id: str, remover_id: str, user_id: str) -> bool:
    """Гишүүн хасах."""
    with session_scope() as s:
        # Хасагч нь owner эсвэл admin эсэхийг шалгах
        remover_role = s.scalars(
            select(WorkspaceMember.role).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == remover_id
            )
        ).first()
        
        if remover_role not in ("owner", "admin"):
            return False
        
        # Хэрэглэгч өөрийгөө хасах боломжгүй (owner бол)
        ws = s.get(Workspace, workspace_id)
        if ws.owner_id == user_id:
            return False
        
        s.execute(
            delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id
            )
        )
        return True


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


def delete_project(project_id: str, user_id: str) -> bool:
    """Project устгах."""
    with session_scope() as s:
        project = s.get(Project, project_id)
        if not project:
            return False
        
        if not check_permission(project.workspace_id, user_id, "admin"):
            return False
        
        s.delete(project)
        return True
