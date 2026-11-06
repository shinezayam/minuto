"""Хэрэглэгчийн толь бичиг — ASR-д зөв бичүүлэх нэр, нэр томьёо ба автомат засварууд."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import UserResponse, VocabularyCreate, VocabularyListResponse, VocabularyTerm
from app.services import vocabulary
from app.services.auth import current_user

router = APIRouter(prefix="/api/vocabulary", tags=["vocabulary"])


@router.get("", response_model=VocabularyListResponse)
def list_terms(user: UserResponse = Depends(current_user)) -> VocabularyListResponse:
    return VocabularyListResponse(terms=vocabulary.list_terms(user.id))


@router.post("", response_model=VocabularyTerm, status_code=201)
def add_term(body: VocabularyCreate, user: UserResponse = Depends(current_user)) -> VocabularyTerm:
    return vocabulary.add_term(user.id, body.term, body.wrong)


@router.delete("/{term_id}", status_code=204, response_model=None)
def delete_term(term_id: str, user: UserResponse = Depends(current_user)) -> None:
    if not vocabulary.delete_term(user.id, term_id):
        raise HTTPException(status_code=404, detail="Term not found")
