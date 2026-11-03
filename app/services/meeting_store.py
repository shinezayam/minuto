from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from app.models.schemas import (
    MeetingCreate,
    MeetingResponse,
    MeetingStatus,
    MeetingUpdate,
)

# In-memory store for Phase 1 (will be replaced by Supabase in Phase 3)
_meetings: Dict[str, dict] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_meeting(data: MeetingCreate) -> MeetingResponse:
    meeting_id = str(uuid.uuid4())
    now = _now()
    meeting = {
        "id": meeting_id,
        "title": data.title,
        "status": MeetingStatus.DRAFT,
        "language": data.language,
        "started_at": None,
        "ended_at": None,
        "duration_ms": 0,
        "created_at": now,
        "updated_at": now,
        "_pause_accumulated_ms": 0,
        "_last_resume_ms": None,
    }
    _meetings[meeting_id] = meeting
    return _to_response(meeting)


def get_meeting(meeting_id: str) -> Optional[MeetingResponse]:
    meeting = _meetings.get(meeting_id)
    if not meeting:
        return None
    return _to_response(meeting)


def list_meetings() -> list[MeetingResponse]:
    return [_to_response(m) for m in sorted(
        _meetings.values(), key=lambda m: m["created_at"], reverse=True
    )]


def update_meeting(meeting_id: str, data: MeetingUpdate) -> Optional[MeetingResponse]:
    meeting = _meetings.get(meeting_id)
    if not meeting:
        return None
    if data.title is not None:
        meeting["title"] = data.title
    meeting["updated_at"] = _now()
    return _to_response(meeting)


def start_meeting(meeting_id: str) -> Optional[MeetingResponse]:
    meeting = _meetings.get(meeting_id)
    if not meeting:
        return None
    meeting["status"] = MeetingStatus.RECORDING
    meeting["started_at"] = _now()
    meeting["_last_resume_ms"] = int(time.time() * 1000)
    meeting["updated_at"] = _now()
    return _to_response(meeting)


def pause_meeting(meeting_id: str) -> Optional[MeetingResponse]:
    meeting = _meetings.get(meeting_id)
    if not meeting:
        return None
    if meeting["status"] != MeetingStatus.RECORDING:
        return None
    now_ms = int(time.time() * 1000)
    last_resume = meeting.get("_last_resume_ms")
    if last_resume:
        meeting["_pause_accumulated_ms"] += now_ms - last_resume
    meeting["_last_resume_ms"] = None
    meeting["status"] = MeetingStatus.PAUSED
    meeting["duration_ms"] = meeting["_pause_accumulated_ms"]
    meeting["updated_at"] = _now()
    return _to_response(meeting)


def resume_meeting(meeting_id: str) -> Optional[MeetingResponse]:
    meeting = _meetings.get(meeting_id)
    if not meeting:
        return None
    if meeting["status"] != MeetingStatus.PAUSED:
        return None
    meeting["status"] = MeetingStatus.RECORDING
    meeting["_last_resume_ms"] = int(time.time() * 1000)
    meeting["updated_at"] = _now()
    return _to_response(meeting)


def end_meeting(meeting_id: str) -> Optional[MeetingResponse]:
    meeting = _meetings.get(meeting_id)
    if not meeting:
        return None
    now_ms = int(time.time() * 1000)
    last_resume = meeting.get("_last_resume_ms")
    if last_resume:
        meeting["_pause_accumulated_ms"] += now_ms - last_resume
    meeting["_last_resume_ms"] = None
    meeting["duration_ms"] = meeting["_pause_accumulated_ms"]
    meeting["ended_at"] = _now()
    meeting["status"] = MeetingStatus.COMPLETED
    meeting["updated_at"] = _now()
    return _to_response(meeting)


def delete_meeting(meeting_id: str) -> bool:
    return _meetings.pop(meeting_id, None) is not None


def get_meeting_raw(meeting_id: str) -> Optional[dict]:
    return _meetings.get(meeting_id)


def _to_response(meeting: dict) -> MeetingResponse:
    return MeetingResponse(
        id=meeting["id"],
        title=meeting["title"],
        status=meeting["status"],
        language=meeting["language"],
        started_at=meeting.get("started_at"),
        ended_at=meeting.get("ended_at"),
        duration_ms=meeting.get("duration_ms", 0),
        created_at=meeting["created_at"],
        updated_at=meeting["updated_at"],
    )
