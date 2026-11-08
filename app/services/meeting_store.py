"""Хурлын мэдээллийг мэдээллийн санд хадгална. Хэрэглэгчийн өгөгдөл owner_id-аар тусгаарлагдана."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update

from app.db import Contact, Meeting, User, session_scope
from app.models.schemas import ContactResponse, MeetingCreate, MeetingResponse, MeetingStatus, MeetingUpdate


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
        participants=list(m.participants or []),
    )


def _get(session, meeting_id: str, owner_id: Optional[str]) -> Optional[Meeting]:
    m = session.get(Meeting, meeting_id)
    if m is None or (owner_id is not None and m.owner_id != owner_id):
        return None  # бусдын хурлыг "олдсонгүй" гэж хариулна
    return m


def _dedupe(names: List[str]) -> List[str]:
    """Давхардсан нэрийг (том жижиг үсэг ялгахгүй) хасна, дарааллыг хадгална."""
    seen: set[str] = set()
    out: List[str] = []
    for n in names:
        n = " ".join(n.split())
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def _remember_contacts(session, owner_id: str, names: List[str]) -> None:
    """Оролцогчдыг хэрэглэгчийн жагсаалтад хадгална — дараагийн хурал дээр дахин бичихгүй."""
    if not names:
        return
    existing = {c.name.casefold(): c for c in session.scalars(select(Contact).where(Contact.owner_id == owner_id))}
    for name in names:
        c = existing.get(name.casefold())
        if c:
            c.last_used_at = _now()
        else:
            c = Contact(owner_id=owner_id, name=name)
            session.add(c)
            existing[name.casefold()] = c


# ---------- CRUD ----------

def create_meeting(owner_id: str, data: MeetingCreate) -> MeetingResponse:
    participants = _dedupe(data.participants)
    with session_scope() as s:
        m = Meeting(
            owner_id=owner_id,
            title=data.title,
            language=data.language,
            status=MeetingStatus.DRAFT.value,
            participants=participants,
        )
        s.add(m)
        _remember_contacts(s, owner_id, participants)
        s.flush()
        return _to_response(m)


def get_meeting(meeting_id: str, owner_id: Optional[str] = None) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        return _to_response(m) if m else None


def list_meetings(owner_id: str, include_archived: bool = False) -> list[MeetingResponse]:
    with session_scope() as s:
        query = select(Meeting).where(Meeting.owner_id == owner_id)
        if not include_archived:
            query = query.where(Meeting.archived == 0)
        rows = s.scalars(query.order_by(Meeting.created_at.desc())).all()
        return [_to_response(m) for m in rows]


def archive_meeting(meeting_id: str, owner_id: str, archive: bool = True) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m:
            return None
        m.archived = 1 if archive else 0
        m.updated_at = _now()
        return _to_response(m)


def update_meeting(meeting_id: str, owner_id: str, data: MeetingUpdate) -> Optional[MeetingResponse]:
    with session_scope() as s:
        m = _get(s, meeting_id, owner_id)
        if not m:
            return None
        if data.title is not None:
            m.title = data.title
        if data.participants is not None:
            m.participants = _dedupe(data.participants)
            _remember_contacts(s, owner_id, m.participants)
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


def update_transcript_segments(meeting_id: str, updates: list[dict]) -> list:
    """Сегментүүдийг засварлах. updates: [{"id": "seg_1", "text": "...", "speaker": "..."}]"""
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        if not m or not m.transcript:
            return []
        
        transcript = list(m.transcript)
        update_map = {u["id"]: u for u in updates}
        
        for seg in transcript:
            seg_id = seg.get("id")
            if seg_id in update_map:
                upd = update_map[seg_id]
                if "text" in upd and upd["text"] is not None:
                    seg["text"] = upd["text"]
                if "speaker" in upd and upd["speaker"] is not None:
                    seg["speaker"] = upd["speaker"]
                # turns дотор засварлах
                if "turns" in seg and isinstance(seg["turns"], list):
                    for turn in seg["turns"]:
                        if "text" in upd and upd["text"] is not None:
                            turn["text"] = upd["text"]
                        if "speaker" in upd and upd["speaker"] is not None:
                            turn["speaker"] = upd["speaker"]
        
        m.transcript = transcript
        m.updated_at = _now()
        return transcript


def get_meeting_transcript(meeting_id: str) -> list:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        return list(m.transcript or []) if m else []


def update_meeting_minutes(meeting_id: str, minutes: dict) -> None:
    with session_scope() as s:
        s.execute(update(Meeting).where(Meeting.id == meeting_id).values(minutes=minutes, updated_at=_now()))


class TranscriptBusyError(Exception):
    """Боловсруулж байх үед бичвэрийг засахгүй (боловсруулалт дарж бичнэ)."""


def replace_in_meeting(
    meeting_id: str,
    find: str,
    replacement: str,
    whole_word: bool = True,
    case_sensitive: bool = False,
    include_minutes: bool = True,
) -> dict:
    """Бичвэр (мөн хүсвэл тэмдэглэл)-д find-ийг бүх газарт нь replacement болгоно."""
    from app.services.text_replace import build_pattern, replace_in_obj, replace_in_segments

    pattern = build_pattern(find, whole_word, case_sensitive)
    with session_scope() as s:
        m = s.get(Meeting, meeting_id, with_for_update=True)
        if not m:
            return {"replaced": 0, "minutes_replaced": 0, "segments": []}
        if m.status == MeetingStatus.PROCESSING.value:
            raise TranscriptBusyError()
        segments, replaced = replace_in_segments(list(m.transcript or []), pattern, replacement)
        values: dict = {}
        if replaced:
            values["transcript"] = segments
        minutes_replaced = 0
        if include_minutes and m.minutes:
            minutes, minutes_replaced = replace_in_obj(dict(m.minutes), pattern, replacement)
            if minutes_replaced:
                values["minutes"] = minutes
        if values:
            s.execute(update(Meeting).where(Meeting.id == meeting_id).values(**values, updated_at=_now()))
        return {"replaced": replaced, "minutes_replaced": minutes_replaced, "segments": segments}


def get_meeting_minutes(meeting_id: str) -> Optional[dict]:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        return dict(m.minutes) if m and m.minutes else None


def list_meeting_contents(
    owner_id: str,
    meeting_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    meeting_ids: Optional[list[str]] = None,
    participant: Optional[str] = None,
) -> list[dict]:
    """Ask Minuto-д: бичвэртэй хурлуудын бичвэр, тэмдэглэл (шинэ нь эхэндээ)."""
    from datetime import datetime
    with session_scope() as s:
        q = select(Meeting).where(Meeting.owner_id == owner_id, Meeting.transcript.is_not(None))
        if meeting_id is not None:
            q = q.where(Meeting.id == meeting_id)
        if meeting_ids:
            q = q.where(Meeting.id.in_(meeting_ids))
        if date_from:
            try:
                dt_from = datetime.fromisoformat(date_from)
                q = q.where(Meeting.created_at >= dt_from)
            except ValueError:
                pass
        if date_to:
            try:
                dt_to = datetime.fromisoformat(date_to + "T23:59:59")
                q = q.where(Meeting.created_at <= dt_to)
            except ValueError:
                pass
        if participant:
            # JSON array-аас оролцогч олох
            q = q.where(Meeting.participants.is_not(None))
            rows = s.scalars(q.order_by(Meeting.created_at.desc())).all()
            # Python дээр шүүх (JSON contains)
            rows = [m for m in rows if m.participants and participant in m.participants]
            return [
                {
                    "id": m.id,
                    "title": m.title,
                    "created_at": m.created_at,
                    "transcript": list(m.transcript or []),
                    "minutes": dict(m.minutes) if m.minutes else None,
                }
                for m in rows
            ]
        rows = s.scalars(q.order_by(Meeting.created_at.desc())).all()
        return [
            {
                "id": m.id,
                "title": m.title,
                "created_at": m.created_at,
                "transcript": list(m.transcript or []),
                "minutes": dict(m.minutes) if m.minutes else None,
            }
            for m in rows
        ]


def get_meeting_context(meeting_id: str) -> dict:
    """Боловсруулалтад: оролцогчид болон тэмдэглэл хөтлөгчийн нэр."""
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            return {"participants": [], "owner_name": None}
        owner = s.get(User, m.owner_id)
        return {"participants": list(m.participants or []), "owner_name": owner_display_name(owner)}


def owner_display_name(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    return (user.name or "").strip() or user.email


# ---------- Байнгын оролцогчид ----------

def list_contacts(owner_id: str) -> List[ContactResponse]:
    with session_scope() as s:
        rows = s.scalars(
            select(Contact).where(Contact.owner_id == owner_id).order_by(Contact.last_used_at.desc(), Contact.name)
        ).all()
        return [ContactResponse(id=c.id, name=c.name) for c in rows]


def delete_contact(contact_id: str, owner_id: str) -> bool:
    with session_scope() as s:
        c = s.get(Contact, contact_id)
        if not c or c.owner_id != owner_id:
            return False
        s.delete(c)
        return True
