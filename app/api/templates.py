"""Хурлын загварын API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from app.models.schemas import UserResponse
from app.services.auth import current_user
from app.services import templates

router = APIRouter(prefix="/api/templates", tags=["templates"])


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    icon: str = Field(default="📋", max_length=10)
    sections: List[str] = Field(default_factory=lambda: ["summary", "topics", "decisions", "action_items"])
    custom_prompt: Optional[str] = None


class TemplateResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    icon: str
    sections: List[str]
    custom_prompt: Optional[str]
    is_default: bool
    owner_id: Optional[str]


@router.get("", response_model=List[TemplateResponse])
def list_templates(user: UserResponse = Depends(current_user)) -> List[TemplateResponse]:
    """Бүх template жагсаах (default + хэрэглэгчийн)."""
    return templates.list_templates(user.id)


@router.post("", response_model=TemplateResponse, status_code=201)
def create_template(body: TemplateCreate, user: UserResponse = Depends(current_user)) -> TemplateResponse:
    """Шинэ template үүсгэх."""
    return templates.create_template(
        owner_id=user.id,
        name=body.name,
        description=body.description,
        icon=body.icon,
        sections=body.sections,
        custom_prompt=body.custom_prompt,
    )


@router.get("/{template_id}", response_model=TemplateResponse)
def get_template(template_id: str, user: UserResponse = Depends(current_user)) -> TemplateResponse:
    """Template-г ID-р авах."""
    tmpl = templates.get_template(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    # Зөвхөн өөрийнх эсвэл default template-г харж болно
    if tmpl["owner_id"] and tmpl["owner_id"] != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return tmpl


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: str, user: UserResponse = Depends(current_user)) -> None:
    """Template устгах (зөвхөн өөрийнх)."""
    if not templates.delete_template(template_id, user.id):
        raise HTTPException(status_code=404, detail="Template not found or cannot be deleted")
