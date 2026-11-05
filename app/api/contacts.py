"""Хэрэглэгчийн байнгын оролцогчид — хурал бүрт дахин бичихгүй сонгоход."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import ContactListResponse, UserResponse
from app.services import meeting_store
from app.services.auth import current_user

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


@router.get("", response_model=ContactListResponse)
def list_contacts(user: UserResponse = Depends(current_user)) -> ContactListResponse:
    return ContactListResponse(contacts=meeting_store.list_contacts(user.id))


@router.delete("/{contact_id}", status_code=204, response_model=None)
def delete_contact(contact_id: str, user: UserResponse = Depends(current_user)) -> None:
    if not meeting_store.delete_contact(contact_id, user.id):
        raise HTTPException(status_code=404, detail="Contact not found")
