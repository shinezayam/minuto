from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import io
import mimetypes
import re
import shutil

from typing import Literal, Optional

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.models.minutes import MeetingMinutes
from app.models.schemas import (
    ActionItemsUpdate,
    MeetingCreate,
    MeetingListResponse,
    MeetingResponse,
    MeetingUpdate,
    TranscriptUpdate,
    TranscriptReplace,
    UserResponse,
)
from app.config import settings
from app.services import live_transcriber, meeting_store, processing, templates, vocabulary
from app.services import audio_store, billing, jobs, minutes_review, transcript_edit
from app.services import upload_guard as guard
from app.services.audio_utils import find_meeting_audio, probe_audio_error
from app.services import auth as auth_service
from app.services.auth import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meetings", tags=["meetings"])

_UPLOAD_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}


def _prepared_by(user: UserResponse) -> str:
    """Хурлын тэмдэглэл хөтөлсөн: Minuto / <хэрэглэгчийн нэр, нэргүй бол имэйл>"""
    return f"Minuto / {(user.name or '').strip() or user.email}"


def _owned(meeting_id: str, user: UserResponse, action: str = "view") -> MeetingResponse:
    """Хурлыг эрхийн дагуу авна: хандах эрхгүй бол 404, харж болох ч action хийх эрхгүй бол 403 (AccessDenied)."""
    meeting = meeting_store.get_meeting(meeting_id, user.id, action)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("", response_model=MeetingResponse, status_code=201)
def create_meeting(body: MeetingCreate, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    if body.template_id:
        tmpl = templates.get_template(body.template_id)
        if not tmpl or not (tmpl["is_default"] or tmpl["owner_id"] == user.id):
            raise HTTPException(status_code=404, detail="TEMPLATE_NOT_FOUND: Загвар олдсонгүй")
    try:
        return meeting_store.create_meeting(user.id, body)
    except meeting_store.WorkspaceAccessError:
        raise HTTPException(status_code=404, detail="Workspace not found")
    except meeting_store.InvalidProject:
        raise HTTPException(status_code=422, detail="INVALID_PROJECT: project_id нь заасан workspace-д харьяалагдахгүй байна")


@router.get("", response_model=MeetingListResponse)
def list_meetings(
    include_archived: bool = Query(False),
    workspace_id: Optional[str] = Query(default=None, max_length=36),
    user: UserResponse = Depends(current_user),
) -> MeetingListResponse:
    return MeetingListResponse(meetings=meeting_store.list_meetings(user.id, include_archived=include_archived, workspace_id=workspace_id))


# ---- Хогийн сав (төлбөртэй багц). /{meeting_id}-ээс өмнө бүртгэнэ ("trash" нь id биш)

@router.get("/trash", response_model=MeetingListResponse)
def list_trash(user: UserResponse = Depends(current_user)) -> MeetingListResponse:
    return MeetingListResponse(meetings=meeting_store.list_trash(user.id))


@router.delete("/trash", status_code=204, response_model=None)
async def empty_trash(user: UserResponse = Depends(current_user)) -> None:
    ids = meeting_store.empty_trash(user.id)
    for mid in ids:
        await asyncio.to_thread(audio_store.purge, mid)


@router.post("/{meeting_id}/restore", response_model=MeetingResponse)
def restore_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.restore_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.get("/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    return _owned(meeting_id, user)


@router.patch("/{meeting_id}", response_model=MeetingResponse)
def update_meeting(meeting_id: str, body: MeetingUpdate, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.update_meeting(meeting_id, user.id, body)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/start", response_model=MeetingResponse)
def start_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.start_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/pause", response_model=MeetingResponse)
def pause_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    live = live_transcriber.get(meeting_id)
    if live is not None and meeting_store.get_meeting(meeting_id, user.id, "edit"):
        live.request_flush()  # зогсоохоос өмнө хэлсэн үгийг шууд харуулна
    meeting = meeting_store.pause_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found or not recording")
    return meeting


@router.post("/{meeting_id}/resume", response_model=MeetingResponse)
def resume_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.resume_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found or not paused")
    return meeting


async def _persist_in_background(meeting_id: str) -> None:
    try:
        await asyncio.to_thread(audio_store.persist, meeting_id)
    except Exception:  # noqa: BLE001 — /process дахин оролдоно
        logger.exception("Background audio persist failed for meeting %s", meeting_id)


@router.post("/{meeting_id}/end", response_model=MeetingResponse)
async def end_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.end_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await live_transcriber.stop(meeting_id)  # бүтэн бичвэрийг /process гаргана
    if audio_store.enabled():
        # Бичлэг дууссан даруй нэг файл болгож байнгын хадгалалт руу хуулна (хариуг удаашруулахгүй)
        asyncio.create_task(_persist_in_background(meeting_id))
    return meeting


@router.delete("/{meeting_id}", status_code=204, response_model=None)
async def delete_meeting(meeting_id: str, permanent: bool = Query(False),
                         user: UserResponse = Depends(current_user)) -> None:
    """Төлбөртэй багцад эхлээд хогийн сав руу (аудио хадгалагдана); хогийн савнаас / permanent=true / Free бол бүрмөсөн."""
    result = meeting_store.delete_meeting(meeting_id, user.id, permanent=permanent)
    if result is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await live_transcriber.stop(meeting_id)
    if result == "deleted":
        # Бүрмөсөн устгахад аудио бичлэг ч устна (remote амжилтгүй бол лог бичиж дараа дахин оролдоно)
        await asyncio.to_thread(audio_store.purge, meeting_id)


@router.post("/{meeting_id}/archive", response_model=MeetingResponse)
def archive_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.archive_meeting(meeting_id, user.id, archive=True)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/unarchive", response_model=MeetingResponse)
def unarchive_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.archive_meeting(meeting_id, user.id, archive=False)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/pin", response_model=MeetingResponse)
def pin_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    """"Анхаарах" гэж тэмдэглэнэ: жагсаалтын хамгийн дээр гарч, "Яаралтай" табад харагдана."""
    try:
        meeting = meeting_store.pin_meeting(meeting_id, user.id, pinned=True)
    except ValueError:
        raise HTTPException(status_code=409, detail="ARCHIVED: Архивласан хурлыг тэмдэглэх боломжгүй. Эхлээд архиваас гаргана уу.")
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/unpin", response_model=MeetingResponse)
def unpin_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.pin_meeting(meeting_id, user.id, pinned=False)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/upload", status_code=200)
async def upload_audio(
    meeting_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: UserResponse = Depends(current_user),
) -> dict:
    _owned(meeting_id, user, "edit")

    try:
        guard.check_declared_size(request.headers.get("content-length"))
        if not guard.declared_type_ok(file.content_type or ""):
            raise HTTPException(status_code=415, detail="UNSUPPORTED_AUDIO: Зөвхөн аудио файл оруулна уу")
        guard.free_disk_ok()
        guard.check_quota(meeting_store.meeting_ids(user.id), incoming=0)
    except guard.GuardError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e

    meeting_dir = processing.meeting_dir(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    part = meeting_dir / "uploaded.part"

    def _save() -> str:
        """Урсгалаар бичиж, байтыг тоолно: хязгаар хэтэрвэл тэр даруй зогсоож хагас файлыг устгана."""
        written = 0
        head = b""
        try:
            with open(part, "wb") as f:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:16]
                    written += len(chunk)
                    if settings.max_upload_bytes and written > settings.max_upload_bytes:
                        raise guard.too_large()
                    f.write(chunk)
            if not written:
                raise guard.GuardError("EMPTY_FILE", 400, "Файл хоосон байна")
            ext = guard.sniff_audio_ext(head)
            if ext is None:
                raise guard.GuardError("UNSUPPORTED_AUDIO", 415, "Энэ файл аудио форматтай биш байна (mp3, wav, m4a, ogg, webm, flac дэмжинэ)")
            guard.check_quota(meeting_store.meeting_ids(user.id), incoming=written)
            return ext
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    try:
        ext = await asyncio.to_thread(_save)
    except guard.GuardError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e

    for old in meeting_dir.glob("uploaded.*"):
        if old != part:
            old.unlink()
    filepath = meeting_dir / f"uploaded{ext}"
    part.replace(filepath)

    # Гэмтсэн/дутуу файлыг Process хүртэл хүлээлгүй, upload дээр нь шууд буцаана
    probe_error = await asyncio.to_thread(probe_audio_error, filepath)
    if probe_error:
        logger.warning("Meeting %s: invalid audio upload: %s", meeting_id, probe_error)
        filepath.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=(
                "INVALID_AUDIO: Аудио файл гэмтсэн эсвэл дутуу байна (бичлэг бүрэн хадгалагдаагүй, "
                "эсвэл дутуу татагдсан байж болно). Файлаа тоглуулж шалгаад дахин upload хийнэ үү."
            ),
        )
    try:
        guard.check_duration(await asyncio.to_thread(guard.probe_duration_sec, filepath))
    except guard.GuardError as e:
        filepath.unlink(missing_ok=True)
        raise HTTPException(status_code=e.status, detail=e.detail) from e

    try:
        await asyncio.to_thread(audio_store.persist, meeting_id)
    except audio_store.StorageUnavailable:
        logger.exception("Audio persist failed for meeting %s", meeting_id)
        filepath.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail="STORAGE_UNAVAILABLE: Аудиог хадгалж чадсангүй. Түр хүлээгээд дахин оролдоно уу.")

    return {
        "meeting_id": meeting_id,
        "filename": filepath.name,
        "size_bytes": filepath.stat().st_size,
        "status": "uploaded",
    }


@router.post("/{meeting_id}/regenerate-summary", status_code=202)
async def regenerate_summary(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    """Зөвхөн summary-г дахин үүсгэнэ (бүх meeting minutes биш)."""
    meeting = _owned(meeting_id, user, "edit")
    transcript_data = meeting_store.get_meeting_transcript(meeting_id)
    if not transcript_data:
        raise HTTPException(status_code=400, detail="NO_TRANSCRIPT: Бичвэр олдсонгүй")
    
    from app.models.minutes import TranscriptSegment
    from app.services import llm as llm_mod
    
    transcript = [TranscriptSegment(**seg) for seg in transcript_data]
    try:
        # Зөвхөн хураангуйг (богино дуудлага) шинэчилнэ; бусад хэсэг хөндөгдөхгүй
        ctx = meeting_store.get_meeting_context(meeting_id)
        summary = await llm_mod.llm_provider.generate_summary(transcript, language=ctx["language"])

        current_minutes = meeting_store.get_meeting_minutes(meeting_id)
        if current_minutes:
            current_minutes["summary"] = summary
            minutes_review.invalidate_approval(current_minutes)  # агуулга өөрчлөгдсөн → дахин батлах шаардлагатай
            meeting_store.update_meeting_minutes(meeting_id, current_minutes)

        return {"meeting_id": meeting_id, "summary": summary}
    except Exception:  # noqa: BLE001 — дэлгэрэнгүйг зөвхөн лог руу бичнэ
        logger.exception("Meeting %s summary regeneration failed", meeting_id)
        raise HTTPException(status_code=500, detail="SUMMARY_FAILED: Хураангуйг дахин үүсгэж чадсангүй")


def _action_items_etag(items) -> str:
    return hashlib.sha1(json.dumps(items or [], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _is_iso_date(value: str) -> bool:
    """YYYY-MM-DD (эсвэл түүнээс эхэлсэн ISO datetime) бодит огноо мөн эсэх."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}(T.*)?$", value):
        return False
    try:
        datetime.date.fromisoformat(value[:10])
        return True
    except ValueError:
        return False


@router.put("/{meeting_id}/action-items")
def update_action_items(
    meeting_id: str,
    body: ActionItemsUpdate,
    user: UserResponse = Depends(current_user),
    if_match: Optional[str] = Header(default=None),
) -> dict:
    """Даалгавруудыг шинэчилнэ. Бүтэц, статус, огноог шалгана; If-Match өгвөл зөрчилдөөнийг (412) илрүүлнэ."""
    _owned(meeting_id, user, "edit")
    current_minutes = meeting_store.get_meeting_minutes(meeting_id)
    if not current_minutes:
        raise HTTPException(status_code=404, detail="NO_MINUTES: Хурлын бичиг үүсээгүй байна")

    current_items = current_minutes.get("action_items", [])
    if if_match and if_match.strip('"') != _action_items_etag(current_items):
        raise HTTPException(status_code=412, detail="CONFLICT: Даалгаврыг өөр газраас өөрчилсөн байна. Дахин ачаалж үзнэ үү.")

    # Хуучин минутын чөлөөт текстэн due_date ("Баасан") өөрчлөгдөөгүй бол тэвчнэ; шинэ утга заавал ISO огноо
    legacy_dates = {str(i.get("due_date")) for i in current_items if isinstance(i, dict) and i.get("due_date")}
    valid_ids = {s.get("id") for s in (meeting_store.get_meeting_transcript(meeting_id) or [])}
    cleaned = []
    for item in body.action_items:
        if item.due_date and not _is_iso_date(item.due_date) and item.due_date not in legacy_dates:
            raise HTTPException(status_code=422, detail="INVALID_DUE_DATE: Хугацаа YYYY-MM-DD хэлбэртэй байх ёстой")
        data = item.model_dump()
        # Транскриптэд байхгүй сегментийн лавлагааг хасна
        data["source_segment_ids"] = [i for i in data["source_segment_ids"] if i in valid_ids]
        cleaned.append(data)

    # Хүний шалгалтын тэмдэглэл, ишлэлийг хадгалах: ижил даалгаврыг (task) олвол өмнөх evidence/review-г хэвээр үлдээнэ
    previous = {str(i.get("task")): i for i in current_items if isinstance(i, dict)}
    for item in cleaned:
        old = previous.get(item["task"])
        if old:
            for key in ("evidence", "verification", "confidence", "review"):
                if key in old:
                    item[key] = old[key]
    current_minutes["action_items"] = cleaned
    minutes_review.invalidate_approval(current_minutes)
    meeting_store.update_meeting_minutes(meeting_id, current_minutes)
    return {"meeting_id": meeting_id, "action_items": cleaned, "etag": _action_items_etag(cleaned)}


@router.post("/{meeting_id}/topics")
async def generate_topics(meeting_id: str, user: UserResponse = Depends(current_user)):
    """Бичвэрийг сэдвүүдэд хуваана"""
    meeting = _owned(meeting_id, user, "edit")
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    
    transcript = meeting_store.get_meeting_transcript(meeting_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="No transcript available")
    
    from app.models.minutes import TranscriptSegment
    from app.services.topic_segmentation import segment_topics
    
    segments = [TranscriptSegment(**s) for s in transcript]
    topics = await segment_topics(segments)
    
    return {
        "meeting_id": meeting_id,
        "topics": [
            {
                "title": t.title,
                "description": t.description,
                "start_ms": t.start_ms,
                "end_ms": t.end_ms,
                "segment_ids": t.segment_ids,
            }
            for t in topics
        ]
    }


@router.post("/{meeting_id}/process", status_code=202)
async def process_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    """
    Боловсруулалтыг ард нь эхлүүлээд шууд хариу өгнө.
    Явцыг GET /api/meetings/{id} (processing_stage, processing_done/total, processing_error)-ээр харна.
    """
    _owned(meeting_id, user, "edit")
    try:
        await asyncio.to_thread(audio_store.ensure_local, meeting_id)
    except audio_store.StorageUnavailable:
        raise HTTPException(status_code=503, detail="STORAGE_UNAVAILABLE: Аудиог хадгалалтаас уншиж чадсангүй. Дахин оролдоно уу.")
    if not processing.has_audio(meeting_id):
        raise HTTPException(status_code=400, detail="NO_AUDIO: No audio file found. Please upload audio first.")
    if audio_store.enabled():
        try:  # шууд бичлэгийн нийлүүлсэн файл remote-д байхгүй бол одоо хуулна
            await asyncio.to_thread(audio_store.persist, meeting_id)
        except audio_store.StorageUnavailable:
            logger.exception("Audio persist before processing failed for meeting %s", meeting_id)
    # Минут: off бол юу ч хийхгүй, shadow бол зөвхөн бүртгэнэ, enforce бол хүрэлцэхгүй үед 402
    try:
        usage_id = await asyncio.to_thread(billing.reserve, meeting_id, user.id)
    except billing.BillingBlocked as e:
        raise HTTPException(status_code=402, detail=e.detail) from e
    if not meeting_store.try_begin_processing(meeting_id):
        if usage_id and not jobs.has_active(meeting_id):
            billing.release(usage_id)
        raise HTTPException(status_code=409, detail="ALREADY_PROCESSING: Энэ хурлыг боловсруулж байна")
    await live_transcriber.stop(meeting_id)
    try:
        processing.start(meeting_id)
    except jobs.AlreadyActive:
        raise HTTPException(status_code=409, detail="ALREADY_PROCESSING: Энэ хурлыг боловсруулж байна")
    return {"meeting_id": meeting_id, "status": "processing"}


@router.get("/{meeting_id}/transcript")
def get_transcript(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    _owned(meeting_id, user)
    segments = meeting_store.get_meeting_transcript(meeting_id)
    return {"meeting_id": meeting_id, "segments": segments, "etag": transcript_edit.etag(segments)}


@router.put("/{meeting_id}/transcript")
def update_transcript(
    meeting_id: str,
    body: TranscriptUpdate,
    user: UserResponse = Depends(current_user),
    if_match: Optional[str] = Header(default=None),
) -> dict:
    """
    Бичвэрийн сегментүүдийг засварлах. Олон хүнтэй сегментэд turn_index (эсвэл бүтэн turns) заавал.
    If-Match: GET-ээс авсан etag — хойшоо өөрчлөгдсөн бол 412. Боловсруулж байвал 409. Өмнөх хувилбар түүхэнд хадгалагдана.
    """
    _owned(meeting_id, user, "edit")
    edits = [s.model_dump(exclude_none=True) for s in body.segments]
    try:
        result = meeting_store.edit_transcript(meeting_id, user.id, edits, if_match)
    except transcript_edit.EditError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e
    except meeting_store.TranscriptBusyError:
        raise HTTPException(status_code=409, detail="ALREADY_PROCESSING: Боловсруулж дуусахыг хүлээнэ үү")
    except meeting_store.TranscriptConflictError:
        raise HTTPException(status_code=412, detail="CONFLICT: Бичвэрийг өөр газраас өөрчилсөн байна. Дахин ачаалж үзнэ үү.")
    except LookupError:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"meeting_id": meeting_id, "segments": result["segments"], "etag": result["etag"]}


@router.get("/{meeting_id}/transcript/revisions")
def list_transcript_revisions(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    _owned(meeting_id, user)
    return {"meeting_id": meeting_id, "revisions": meeting_store.list_revisions(meeting_id)}


@router.post("/{meeting_id}/transcript/revisions/{revision_id}/restore")
def restore_transcript_revision(meeting_id: str, revision_id: str, user: UserResponse = Depends(current_user)) -> dict:
    """Түүхээс бичвэрийг сэргээнэ. Одоогийн бичвэр мөн түүхэнд хадгалагдана (сэргээлтийг буцаах боломжтой)."""
    _owned(meeting_id, user, "edit")
    try:
        result = meeting_store.restore_revision(meeting_id, user.id, revision_id)
    except meeting_store.TranscriptBusyError:
        raise HTTPException(status_code=409, detail="ALREADY_PROCESSING: Боловсруулж дуусахыг хүлээнэ үү")
    except LookupError:
        raise HTTPException(status_code=404, detail="Revision not found")
    return {"meeting_id": meeting_id, "segments": result["segments"], "etag": result["etag"]}


@router.post("/{meeting_id}/transcript/replace")
def replace_in_transcript(meeting_id: str, body: TranscriptReplace, user: UserResponse = Depends(current_user)) -> dict:
    """Буруу танигдсан үгийг бичвэр (include_minutes үед тэмдэглэл)-ийн бүх газарт солино."""
    _owned(meeting_id, user, "edit")
    try:
        result = meeting_store.replace_in_meeting(
            meeting_id,
            body.find,
            body.replace,
            whole_word=body.whole_word,
            case_sensitive=body.case_sensitive,
            include_minutes=body.include_minutes,
        )
    except meeting_store.TranscriptBusyError:
        raise HTTPException(status_code=409, detail="ALREADY_PROCESSING: Боловсруулж дуусахыг хүлээнэ үү")
    remembered = False
    if body.remember and body.replace and (result["replaced"] or result["minutes_replaced"]):
        vocabulary.remember_correction(user.id, body.find, body.replace)
        remembered = True
    return {"meeting_id": meeting_id, **result, "remembered": remembered}


@router.get("/{meeting_id}/minutes")
def get_minutes(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingMinutes:
    meeting = _owned(meeting_id, user)
    minutes = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes:
        # Шалтгааныг нууж болохгүй: боловсруулалт унасан бол яг ямар алдаа гарсныг буцаана
        if meeting.processing_error:
            detail = f"Minutes not generated: {meeting.processing_error}"
        elif meeting.processing_stage and meeting.processing_stage not in ("done", "failed"):
            detail = f"PROCESSING: Боловсруулж байна ({meeting.processing_stage}). Дуусахыг хүлээнэ үү."
        else:
            detail = "Minutes not generated yet. Process the meeting first."
        raise HTTPException(status_code=404, detail=detail)
    return MeetingMinutes(**{**minutes, "prepared_by": _prepared_by(user)})


class MinutesStatusBody(BaseModel):
    status: Literal["draft", "in_review", "approved"]


@router.put("/{meeting_id}/minutes/status")
def set_minutes_status(meeting_id: str, body: MinutesStatusBody, user: UserResponse = Depends(current_user)) -> dict:
    """Минутын төлөв: draft → in_review → approved. Батлах нь зөвхөн эзэн/админ. Агуулга өөрчлөгдвөл батлалт автоматаар цуцлагдана."""
    meeting = _owned(meeting_id, user, "edit")
    minutes = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes:
        raise HTTPException(status_code=404, detail="NO_MINUTES: Хурлын бичиг үүсээгүй байна")
    try:
        minutes_review.set_status(minutes, body.status, user.name or user.email, can_approve=meeting.my_role in ("owner", "admin"))
    except minutes_review.ReviewError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e
    meeting_store.update_meeting_minutes(meeting_id, minutes)
    return {"meeting_id": meeting_id, "minutes_status": minutes["minutes_status"], "approved_by": minutes.get("approved_by"), "approved_at": minutes.get("approved_at")}


class ItemReviewBody(BaseModel):
    decision: Optional[Literal["accepted", "edited", "rejected"]] = None
    text: Optional[str] = Field(default=None, max_length=1000)


@router.put("/{meeting_id}/minutes/{kind}/{index}/review")
def review_minutes_item(meeting_id: str, kind: str, index: int, body: ItemReviewBody, user: UserResponse = Depends(current_user)) -> dict:
    """Шийдвэр/даалгавар бүрийг хүн хүлээн авах (accepted), засах (edited, text-тэй) эсвэл татгалзах (rejected)."""
    _owned(meeting_id, user, "edit")
    minutes = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes:
        raise HTTPException(status_code=404, detail="NO_MINUTES: Хурлын бичиг үүсээгүй байна")
    try:
        minutes_review.review_item(minutes, kind, index, body.decision, body.text)
    except minutes_review.ReviewError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e
    meeting_store.update_meeting_minutes(meeting_id, minutes)
    return {"meeting_id": meeting_id, "minutes": MeetingMinutes(**minutes).model_dump(exclude={"prepared_by"})}


@router.post("/{meeting_id}/ws-ticket")
def create_ws_ticket(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    """WebSocket холболтод зориулсан ≤60 сек, нэг удаагийн, зөвхөн энэ хуралд хүчинтэй тасалбар."""
    _owned(meeting_id, user, "edit")
    ttl = settings.ws_ticket_ttl_sec
    return {"ticket": auth_service.create_ticket(user.id, meeting_id, "ws", ttl), "expires_in": ttl}


@router.post("/{meeting_id}/audio-url")
def create_audio_url(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    """<audio>-д зориулсан богино настай гарын үсэгтэй линк (нэвтрэлтийн токен URL-д орохгүй)."""
    _owned(meeting_id, user)
    ttl = settings.audio_url_ttl_sec
    sig = auth_service.create_ticket(user.id, meeting_id, "audio", ttl)
    return {"url": f"/api/meetings/{meeting_id}/audio?sig={sig}", "expires_in": ttl}


@router.get("/{meeting_id}/audio")
async def get_audio(meeting_id: str, request: Request):
    # Өөрөө нэвтрэлт шалгана: Authorization header, эсвэл ?sig= (гарын үсэгтэй, хугацаатай линк), эсвэл (шилжилтийн хугацаанд) ?token=
    user = auth_service.verify_ticket(request.query_params.get("sig"), "audio", meeting_id)
    if user is None:
        user = auth_service.user_from_token(auth_service.token_from_request(request))
    if user is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED: Нэвтэрнэ үү", headers={"WWW-Authenticate": "Bearer"})
    _owned(meeting_id, user)
    try:
        await asyncio.to_thread(audio_store.ensure_local, meeting_id)
    except audio_store.StorageUnavailable:
        raise HTTPException(status_code=503, detail="STORAGE_UNAVAILABLE: Аудиог хадгалалтаас уншиж чадсангүй. Дахин оролдоно уу.")
    audio = await asyncio.to_thread(find_meeting_audio, processing.meeting_dir(meeting_id))
    if not audio:
        raise HTTPException(status_code=404, detail="No audio file found")
    media_type = mimetypes.guess_type(audio.name)[0] or ("audio/webm" if audio.suffix == ".webm" else "application/octet-stream")
    return FileResponse(audio, media_type=media_type, filename=f"meeting_{meeting_id}{audio.suffix}")


@router.get("/{meeting_id}/export.docx")
def export_docx(meeting_id: str, user: UserResponse = Depends(current_user)):
    meeting = _owned(meeting_id, user)
    minutes_data = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes_data:
        raise HTTPException(status_code=404, detail="Minutes not generated yet")

    from app.services.export_docx import generate_meeting_docx

    docx_bytes = generate_meeting_docx(
        meeting_id=meeting_id,
        title=meeting.title,
        created_at=str(meeting.created_at),
        minutes=MeetingMinutes(**{**minutes_data, "prepared_by": _prepared_by(user)}),
        transcript=meeting_store.get_meeting_transcript(meeting_id),
        participants=meeting.participants,
    )
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=meeting_{meeting_id}.docx"},
    )


@router.get("/{meeting_id}/export.pdf")
def export_pdf(meeting_id: str, user: UserResponse = Depends(current_user)):
    meeting = _owned(meeting_id, user)
    minutes_data = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes_data:
        raise HTTPException(status_code=404, detail="Minutes not generated yet")

    from app.services.export_pdf import generate_meeting_pdf

    pdf_bytes = generate_meeting_pdf(
        meeting_id=meeting_id,
        title=meeting.title,
        created_at=str(meeting.created_at),
        minutes=MeetingMinutes(**{**minutes_data, "prepared_by": _prepared_by(user)}),
        transcript=meeting_store.get_meeting_transcript(meeting_id),
        participants=meeting.participants,
    )
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=meeting_{meeting_id}.pdf"},
    )
