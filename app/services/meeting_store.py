"""Хурлын мэдээллийг мэдээллийн санд хадгална. Хэрэглэгчийн өгөгдөл owner_id-аар тусгаарлагдана."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update

from app.db import Meeting, session_scope
from app.models.schemas import MeetingCreate, MeetingResponse, MeetingStatus, MeetingUpdate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_response(m: Meeting) -> MeetingResponse:
    return MeetingResponse(
        id=m.id,
        title=m.title,
        status=MeetingStatus(m.status),
        language=m.language,
        started_at=m.started_at,
        ended_at=m.ended_at,
        duration_ms=m.duration_ms or 0,
        created_at=m.created_at,
        updated_at=m.updated_at,
        processing_stage=m.processing_stage,
        processing_done=m.processing_done or 0,
        processing_total=m.processing_total or 0,
        processing_error=m.processing_error,
    )


def _get(session, meeting_id: str, owner_id: Optional[str]) -> Optional[Meeting]:
    m = session.get(Meeting, meeting_id)
    if m is None or (owner_id is not None and m.owner_id != owner_id):
        return None  # бусдын хурлыг "олдсонгүй" гэж хариулна
    return m


# ---------- CRUD ----------

def create_meeting(owner_id: str, data: MeetingCreate) -> MeetingResponse:
    with session_scope() as s:
        m = Meeting(owner_id=owner_id, title=data.title, language=data.language, status=MeetingStatus.DRAFT.value)
        s.add(m)
        s.flush()
        return _to_response(m)


def get_meeting(meeting_id: str, owner_id: Optional[str] = None) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        return _to_response(m) if m else None


def list_meetings(owner_id: str) -> list[MeetingResponse]:
    with session_scope() as s:
        rows = s.scalars(
            select(Meeting).where(Meeting.owner_id == owner_id).order_by(Meeting.created_at.desc())
        ).all()
        return [_to_response(m) for m in rows]


def update_meeting(meeting_id: str, owner_id: str, data: MeetingUpdate) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m:
            return None
        if data.title is not None:
            m.title = data.title
        m.updated_at = _now()
        return _to_response(m)


def delete_meeting(meeting_id: str, owner_id: str) -> bool:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m:
            return False
        s.delete(m)
        return True


# ---------- Бичлэгийн төлөв ----------

def start_meeting(meeting_id: str, owner_id: str) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m:
            return None
        m.status = MeetingStatus.RECORDING.value
        m.started_at = m.started_at or _now()
        m.last_resume_ms = _now_ms()
        m.updated_at = _now()
        return _to_response(m)


def pause_meeting(meeting_id: str, owner_id: str) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m or m.status != MeetingStatus.RECORDING.value:
            return None
        if m.last_resume_ms:
            m.pause_accumulated_ms = (m.pause_accumulated_ms or 0) + _now_ms() - m.last_resume_ms
        m.last_resume_ms = None
        m.status = MeetingStatus.PAUSED.value
        m.duration_ms = m.pause_accumulated_ms
        m.updated_at = _now()
        return _to_response(m)


def resume_meeting(meeting_id: str, owner_id: str) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m or m.status != MeetingStatus.PAUSED.value:
            return None
        m.status = MeetingStatus.RECORDING.value
        m.last_resume_ms = _now_ms()
        m.updated_at = _now()
        return _to_response(m)


def end_meeting(meeting_id: str, owner_id: str) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m:
            return None
        if m.last_resume_ms:
            m.pause_accumulated_ms = (m.pause_accumulated_ms or 0) + _now_ms() - m.last_resume_ms
        m.last_resume_ms = None
        m.duration_ms = m.pause_accumulated_ms or 0
        m.ended_at = _now()
        m.status = MeetingStatus.COMPLETED.value
        m.updated_at = _now()
        return _to_response(m)


# ---------- Боловсруулалт ----------

def try_begin_processing(meeting_id: str) -> bool:
    """Боловсруулж эхэлнэ. Аль хэдийн боловсруулж байвал False (давхар ажиллуулахгүй)."""
    with session_scope() as s:
        result = s.execute(
            update(Meeting)
            .where(Meeting.id == meeting_id, Meeting.status != MeetingStatus.PROCESSING.value)
            .values(
                status=MeetingStatus.PROCESSING.value,
                processing_stage="queued",
                processing_done=0,
                processing_total=0,
                processing_error=None,
                updated_at=_now(),
            )
        )
        return result.rowcount == 1


def set_progress(meeting_id: str, stage: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
    values: dict = {"processing_stage": stage, "updated_at": _now()}
    if done is not None:
        values["processing_done"] = done
    if total is not None:
        values["processing_total"] = total
    with session_scope() as s:
        s.execute(update(Meeting).where(Meeting.id == meeting_id).values(**values))


def finish_processing(meeting_id: str, error: Optional[str] = None) -> None:
    with session_scope() as s:
        s.execute(
            update(Meeting)
            .where(Meeting.id == meeting_id)
            .values(
                status=(MeetingStatus.FAILED if error else MeetingStatus.COMPLETED).value,
                processing_stage="failed" if error else "done",
                processing_error=error,
                updated_at=_now(),
            )
        )


def fail_interrupted_processing() -> int:
    """Сервер дахин эхлэхэд дутуу үлдсэн боловсруулалтыг алдаатай гэж тэмдэглэнэ."""
    with session_scope() as s:
        result = s.execute(
            update(Meeting)
            .where(Meeting.status == MeetingStatus.PROCESSING.value)
            .values(
                status=MeetingStatus.FAILED.value,
                processing_stage="failed",
                processing_error="INTERRUPTED: Сервер дахин эхэлсэн тул боловсруулалт тасарсан. Дахин оролдоно уу.",
                updated_at=_now(),
            )
        )
        return result.rowcount


# ---------- Бичвэр, тэмдэглэл ----------

def update_meeting_transcript(meeting_id: str, transcript: list) -> None:
    with session_scope() as s:
        s.execute(update(Meeting).where(Meeting.id == meeting_id).values(transcript=transcript, updated_at=_now()))


def get_meeting_transcript(meeting_id: str) -> list:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        return list(m.transcript or []) if m else []


def update_meeting_minutes(meeting_id: str, minutes: dict) -> None:
    with session_scope() as s:
        s.execute(update(Meeting).where(Meeting.id == meeting_id).values(minutes=minutes, updated_at=_now()))


def get_meeting_minutes(meeting_id: str) -> Optional[dict]:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        return dict(m.minutes) if m and m.minutes else None
