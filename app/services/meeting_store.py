"""Хурлын мэдээллийг мэдээллийн санд хадгална. Хэрэглэгчийн өгөгдөл owner_id-аар тусгаарлагдана."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, select, update

from app.db import Contact, Meeting, MeetingTemplate, ProcessingJob, TranscriptRevision, User, session_scope
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
        template_id=m.template_id,
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
            template_id=data.template_id,
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


def meeting_ids(owner_id: str) -> list[str]:
    """Хэрэглэгчийн бүх (архивласан ч) хурлын id — квот тооцоход."""
    with session_scope() as s:
        return list(s.scalars(select(Meeting.id).where(Meeting.owner_id == owner_id)))


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
            .where(
                Meeting.id == meeting_id,
                # Идэвхтэй бичлэг дундуур эсвэл аль хэдийн боловсруулж байхад эхлүүлэхгүй
                Meeting.status.notin_((MeetingStatus.PROCESSING.value, MeetingStatus.RECORDING.value, MeetingStatus.PAUSED.value)),
            )
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
            .where(
                Meeting.status == MeetingStatus.PROCESSING.value,
                ~select(ProcessingJob.id).where(
                    ProcessingJob.meeting_id == Meeting.id, ProcessingJob.state.in_(("queued", "running"))
                ).exists(),
            )
            .values(
                status=MeetingStatus.FAILED.value,
                processing_stage="failed",
                processing_error="INTERRUPTED: Сервер дахин эхэлсэн тул боловсруулалт тасарсан. Дахин оролдоно уу.",
                updated_at=_now(),
            )
        )
        return result.rowcount


# ---------- Бичвэр, тэмдэглэл ----------

def update_meeting_transcript(meeting_id: str, transcript: list, snapshot_kind: Optional[str] = None) -> None:
    """snapshot_kind өгвөл (жишээ нь "reprocess") дарж бичихээс өмнөх бичвэрийг түүхэнд хадгална — хэрэглэгчийн засвар алга болохгүй."""
    with session_scope() as s:
        if snapshot_kind:
            m = s.get(Meeting, meeting_id)
            if m and m.transcript:
                _snapshot(s, meeting_id, list(m.transcript), None, snapshot_kind)
        s.execute(update(Meeting).where(Meeting.id == meeting_id).values(transcript=transcript, updated_at=_now()))


MAX_REVISIONS = 30  # нэг хурлын түүхийн дээд тоо (хуучнаас нь устгана)


def _snapshot(s, meeting_id: str, transcript: Optional[list], user_id: Optional[str], kind: str, changed: Optional[list] = None) -> None:
    """Өөрчлөлтөөс ӨМНӨХ бичвэрийг түүхэнд хадгална; хязгаараас хэтэрвэл хуучныг нь устгана."""
    if not transcript:
        return
    s.add(TranscriptRevision(meeting_id=meeting_id, user_id=user_id, kind=kind, changed=changed or [], transcript=list(transcript)))
    s.flush()
    old = s.scalars(
        select(TranscriptRevision.id)
        .where(TranscriptRevision.meeting_id == meeting_id)
        .order_by(TranscriptRevision.created_at.desc(), TranscriptRevision.id)
        .offset(MAX_REVISIONS)
    ).all()
    if old:
        s.execute(delete(TranscriptRevision).where(TranscriptRevision.id.in_(old)))


def edit_transcript(meeting_id: str, owner_id: str, edits: list[dict], if_match: Optional[str] = None) -> dict:
    """Сегмент засварлах: боловсруулж буй үед татгалзана, зөрчилдөөнийг шалгана, түүх хадгална. Буцаах: {segments, etag}."""
    from app.services.transcript_edit import apply_edits, etag

    with session_scope() as s:
        m = s.get(Meeting, meeting_id, with_for_update=True)
        if not m or m.owner_id != owner_id:
            raise LookupError(meeting_id)
        if m.status == MeetingStatus.PROCESSING.value:
            raise TranscriptBusyError()
        current = list(m.transcript or [])
        if if_match and if_match.strip('"') != etag(current):
            raise TranscriptConflictError()
        updated, changed = apply_edits(current, edits)
        if changed:
            _snapshot(s, meeting_id, current, owner_id, "edit", changed)
            m.transcript = updated
            m.updated_at = _now()
        return {"segments": updated, "etag": etag(updated), "changed": changed}


def list_revisions(meeting_id: str) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(
            select(TranscriptRevision)
            .where(TranscriptRevision.meeting_id == meeting_id)
            .order_by(TranscriptRevision.created_at.desc(), TranscriptRevision.id)
        ).all()
        return [
            {"id": r.id, "kind": r.kind, "user_id": r.user_id, "created_at": r.created_at, "changed": list(r.changed or []), "segments": len(r.transcript or [])}
            for r in rows
        ]


def restore_revision(meeting_id: str, owner_id: str, revision_id: str) -> dict:
    """Сонгосон хувилбарыг сэргээнэ; одоогийн бичвэрийг мөн түүхэнд хадгалж, сэргээлтийг буцаах боломжтой болгоно."""
    from app.services.transcript_edit import etag

    with session_scope() as s:
        m = s.get(Meeting, meeting_id, with_for_update=True)
        if not m or m.owner_id != owner_id:
            raise LookupError(meeting_id)
        if m.status == MeetingStatus.PROCESSING.value:
            raise TranscriptBusyError()
        rev = s.get(TranscriptRevision, revision_id)
        if not rev or rev.meeting_id != meeting_id:
            raise LookupError(revision_id)
        _snapshot(s, meeting_id, list(m.transcript or []), owner_id, "restore")
        m.transcript = list(rev.transcript)
        m.updated_at = _now()
        return {"segments": list(rev.transcript), "etag": etag(list(rev.transcript))}


def get_meeting_transcript(meeting_id: str) -> list:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        return list(m.transcript or []) if m else []


def update_meeting_minutes(meeting_id: str, minutes: dict) -> None:
    with session_scope() as s:
        s.execute(update(Meeting).where(Meeting.id == meeting_id).values(minutes=minutes, updated_at=_now()))


class TranscriptBusyError(Exception):
    """Боловсруулж байх үед бичвэрийг засахгүй (боловсруулалт дарж бичнэ)."""


class TranscriptConflictError(Exception):
    """Засвар хийж эхлэхээс хойш бичвэр өөр газраас өөрчлөгдсөн."""


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
            _snapshot(s, meeting_id, list(m.transcript or []), m.owner_id, "replace")
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
            return {"participants": [], "owner_name": None, "template": None, "language": "mn"}
        owner = s.get(User, m.owner_id)
        tmpl = s.get(MeetingTemplate, m.template_id) if m.template_id else None
        return {
            "participants": list(m.participants or []),
            "owner_name": owner_display_name(owner),
            "language": m.language or "mn",
            "template": (
                {"name": tmpl.name, "sections": list(tmpl.sections or []), "custom_prompt": tmpl.custom_prompt}
                if tmpl
                else None
            ),
        }


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
