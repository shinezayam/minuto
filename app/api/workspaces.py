"""Workspace API - багийн хамтын ажиллагаа."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from app.models.schemas import UserResponse
from app.services.auth import current_user
from app.services import workspaces

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    workspace_type: str
    owner_id: str
    role: str


class MemberResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str]
    role: str
    joined_at: Optional[str]


class InviteRequest(BaseModel):
    email: str
    role: str = Field(default="viewer", pattern="^(admin|editor|viewer)$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    color: str = Field(default="#5965D8", pattern="^#[0-9A-Fa-f]{6}$")


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    color: str
    workspace_id: str


@router.get("", response_model=List[WorkspaceResponse])
def list_workspaces(user: UserResponse = Depends(current_user)) -> List[WorkspaceResponse]:
    """Хэрэглэгчийн бүх workspace-уудыг жагсаах."""
    return workspaces.list_user_workspaces(user.id)


@router.post("", response_model=WorkspaceResponse, status_code=201)
def create_workspace(body: WorkspaceCreate, user: UserResponse = Depends(current_user)) -> WorkspaceResponse:
    """Шинэ organization workspace үүсгэх."""
    return workspaces.create_organization_workspace(user.id, body.name, body.description)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
def get_workspace(workspace_id: str, user: UserResponse = Depends(current_user)) -> WorkspaceResponse:
    """Workspace-г авах."""
    ws = workspaces.get_workspace(workspace_id, user.id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@router.get("/{workspace_id}/members", response_model=List[MemberResponse])
def list_members(workspace_id: str, user: UserResponse = Depends(current_user)) -> List[MemberResponse]:
    """Workspace-ийн гишүүдийг жагсаах."""
    members = workspaces.list_members(workspace_id, user.id)
    if not members and not workspaces.get_workspace(workspace_id, user.id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return members


@router.post("/{workspace_id}/invite", response_model=MemberResponse)
def invite_member(workspace_id: str, body: InviteRequest, user: UserResponse = Depends(current_user)) -> MemberResponse:
    """Гишүүн урих."""
    result = workspaces.invite_member(workspace_id, user.id, body.email, body.role)
    if not result:
        raise HTTPException(status_code=400, detail="Cannot invite user")
    return result


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(workspace_id: str, user_id: str, user: UserResponse = Depends(current_user)) -> None:
    """Гишүүн хасах."""
    if not workspaces.remove_member(workspace_id, user.id, user_id):
        raise HTTPException(status_code=400, detail="Cannot remove member")


# Projects


@router.get("/{workspace_id}/projects", response_model=List[ProjectResponse])
def list_projects(workspace_id: str, user: UserResponse = Depends(current_user)) -> List[ProjectResponse]:
    """Workspace-ийн project-уудыг жагсаах."""
    projects = workspaces.list_projects(workspace_id, user.id)
    if not projects and not workspaces.get_workspace(workspace_id, user.id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return projects


@router.post("/{workspace_id}/projects", response_model=ProjectResponse, status_code=201)
def create_project(workspace_id: str, body: ProjectCreate, user: UserResponse = Depends(current_user)) -> ProjectResponse:
    """Project үүсгэх."""
    result = workspaces.create_project(workspace_id, user.id, body.name, body.description, body.color)
    if not result:
        raise HTTPException(status_code=403, detail="Permission denied")
    return result


@router.delete("/{workspace_id}/projects/{project_id}", status_code=204)
def delete_project(workspace_id: str, project_id: str, user: UserResponse = Depends(current_user)) -> None:
    """Project устгах."""
    if not workspaces.delete_project(project_id, user.id):
        raise HTTPException(status_code=403, detail="Permission denied")
