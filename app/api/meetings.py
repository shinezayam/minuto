from __future__ import annotations

import asyncio
import io
import mimetypes
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.models.minutes import MeetingMinutes
from app.models.schemas import (
    MeetingCreate,
    MeetingListResponse,
    MeetingResponse,
    MeetingUpdate,
    UserResponse,
)
from app.services import live_transcriber, meeting_store, processing
from app.services.audio_utils import find_meeting_audio, probe_audio_error
from app.services.auth import current_user

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


def _owned(meeting_id: str, user: UserResponse) -> MeetingResponse:
    meeting = meeting_store.get_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("", response_model=MeetingResponse, status_code=201)
def create_meeting(body: MeetingCreate, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    return meeting_store.create_meeting(user.id, body)


@router.get("", response_model=MeetingListResponse)
def list_meetings(user: UserResponse = Depends(current_user)) -> MeetingListResponse:
    return MeetingListResponse(meetings=meeting_store.list_meetings(user.id))


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
    if live is not None and meeting_store.get_meeting(meeting_id, user.id):
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


@router.post("/{meeting_id}/end", response_model=MeetingResponse)
async def end_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> MeetingResponse:
    meeting = meeting_store.end_meeting(meeting_id, user.id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await live_transcriber.stop(meeting_id)  # бүтэн бичвэрийг /process гаргана
    return meeting


@router.delete("/{meeting_id}", status_code=204, response_model=None)
async def delete_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> None:
    if not meeting_store.delete_meeting(meeting_id, user.id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    await live_transcriber.stop(meeting_id)
    # Хурлыг устгахад аудио бичлэг ч устна
    shutil.rmtree(processing.meeting_dir(meeting_id), ignore_errors=True)


@router.post("/{meeting_id}/upload", status_code=200)
async def upload_audio(
    meeting_id: str,
    file: UploadFile = File(...),
    user: UserResponse = Depends(current_user),
) -> dict:
    _owned(meeting_id, user)

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if not content_type.startswith("audio/") and not content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")

    meeting_dir = processing.meeting_dir(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    for old in meeting_dir.glob("uploaded*"):
        old.unlink()
    filepath = meeting_dir / f"uploaded{_UPLOAD_EXT.get(content_type, '.bin')}"

    def _save() -> None:
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

    await asyncio.to_thread(_save)

    # Гэмтсэн/дутуу файлыг Process хүртэл хүлээлгүй, upload дээр нь шууд буцаана
    probe_error = await asyncio.to_thread(probe_audio_error, filepath)
    if probe_error:
        filepath.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=(
                "INVALID_AUDIO: Аудио файл гэмтсэн эсвэл дутуу байна (бичлэг бүрэн хадгалагдаагүй, "
                "эсвэл дутуу татагдсан байж болно). Файлаа тоглуулж шалгаад дахин upload хийнэ үү. "
                f"[{probe_error}]"
            ),
        )

    return {
        "meeting_id": meeting_id,
        "filename": filepath.name,
        "size_bytes": filepath.stat().st_size,
        "status": "uploaded",
    }


@router.post("/{meeting_id}/process", status_code=202)
async def process_meeting(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    """
    Боловсруулалтыг ард нь эхлүүлээд шууд хариу өгнө.
    Явцыг GET /api/meetings/{id} (processing_stage, processing_done/total, processing_error)-ээр харна.
    """
    _owned(meeting_id, user)
    if not processing.has_audio(meeting_id):
        raise HTTPException(status_code=400, detail="NO_AUDIO: No audio file found. Please upload audio first.")
    if not meeting_store.try_begin_processing(meeting_id):
        raise HTTPException(status_code=409, detail="ALREADY_PROCESSING: Энэ хурлыг боловсруулж байна")
    await live_transcriber.stop(meeting_id)
    processing.start(meeting_id)
    return {"meeting_id": meeting_id, "status": "processing"}


@router.get("/{meeting_id}/transcript")
def get_transcript(meeting_id: str, user: UserResponse = Depends(current_user)) -> dict:
    _owned(meeting_id, user)
    return {"meeting_id": meeting_id, "segments": meeting_store.get_meeting_transcript(meeting_id)}


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
    return MeetingMinutes(**minutes)


@router.get("/{meeting_id}/audio")
async def get_audio(meeting_id: str, user: UserResponse = Depends(current_user)):
    _owned(meeting_id, user)
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
        minutes=MeetingMinutes(**minutes_data),
        transcript=meeting_store.get_meeting_transcript(meeting_id),
    )
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=meeting_{meeting_id}.docx"},
    )
