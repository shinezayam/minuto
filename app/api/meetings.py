from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile, File, Form

from app.models.schemas import (
    MeetingCreate,
    MeetingListResponse,
    MeetingResponse,
    MeetingUpdate,
)
from app.services import meeting_store

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


@router.post("", response_model=MeetingResponse, status_code=201)
def create_meeting(body: MeetingCreate) -> MeetingResponse:
    return meeting_store.create_meeting(body)


@router.get("", response_model=MeetingListResponse)
def list_meetings() -> MeetingListResponse:
    return MeetingListResponse(meetings=meeting_store.list_meetings())


@router.get("/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: str) -> MeetingResponse:
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.patch("/{meeting_id}", response_model=MeetingResponse)
def update_meeting(meeting_id: str, body: MeetingUpdate) -> MeetingResponse:
    meeting = meeting_store.update_meeting(meeting_id, body)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/start", response_model=MeetingResponse)
def start_meeting(meeting_id: str) -> MeetingResponse:
    meeting = meeting_store.start_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/pause", response_model=MeetingResponse)
def pause_meeting(meeting_id: str) -> MeetingResponse:
    meeting = meeting_store.pause_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found or not recording")
    return meeting


@router.post("/{meeting_id}/resume", response_model=MeetingResponse)
def resume_meeting(meeting_id: str) -> MeetingResponse:
    meeting = meeting_store.resume_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found or not paused")
    return meeting


@router.post("/{meeting_id}/end", response_model=MeetingResponse)
def end_meeting(meeting_id: str) -> MeetingResponse:
    meeting = meeting_store.end_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.delete("/{meeting_id}", status_code=204, response_model=None)
def delete_meeting(meeting_id: str) -> None:
    if not meeting_store.delete_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")


@router.post("/{meeting_id}/upload", status_code=200)
async def upload_audio(
    meeting_id: str,
    file: UploadFile = File(...),
) -> dict:
    """Upload an audio file for a meeting."""
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")

    # Save the uploaded file
    from app.config import settings
    meeting_dir = Path(settings.audio_storage_path) / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    # Determine extension from content type
    ext = ".webm"
    if file.content_type == "audio/mpeg":
        ext = ".mp3"
    elif file.content_type == "audio/wav":
        ext = ".wav"
    elif file.content_type == "audio/mp4" or file.content_type == "audio/m4a":
        ext = ".m4a"
    elif file.content_type == "audio/ogg":
        ext = ".ogg"

    filepath = meeting_dir / f"uploaded{ext}"

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return {
        "meeting_id": meeting_id,
        "filename": filepath.name,
        "size_bytes": filepath.stat().st_size,
        "status": "uploaded",
    }
