from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import AskRequest, AskResponse, UserResponse
from app.services import ask as ask_service
from app.services import meeting_store
from app.services.asr import ProviderError
from app.services.auth import current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ask", tags=["ask"])


@router.post("", response_model=AskResponse)
async def ask(body: AskRequest, user: UserResponse = Depends(current_user)) -> AskResponse:
    """Хэрэглэгчийн хурлуудаас (meeting_id өгвөл зөвхөн тэр хурлаас) асуулт асууна."""
    if body.meeting_id and not meeting_store.get_meeting(body.meeting_id, user.id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    try:
        return await ask_service.ask(
            user.id,
            body.question.strip(),
            body.meeting_id,
            body.history,
            date_from=body.date_from,
            date_to=body.date_to,
            meeting_ids=body.meeting_ids,
            participant=body.participant,
        )
    except ProviderError as e:
        logger.warning("ask failed: %s", e)
        raise HTTPException(status_code=502, detail="ASK_FAILED: AI хариулт өгч чадсангүй. Дахин оролдоно уу.") from e
