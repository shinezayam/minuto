from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Response, UploadFile, File, Form
from fastapi.responses import FileResponse

from app.models.schemas import (
    MeetingCreate,
    MeetingListResponse,
    MeetingResponse,
    MeetingUpdate,
)
from app.models.minutes import MeetingMinutes, TranscriptSegment
from app.services import meeting_store
from app.services.asr import asr_provider, llm_provider

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


@router.post("/{meeting_id}/process")
async def process_meeting(meeting_id: str) -> dict:
    """Process uploaded audio: transcribe and generate meeting minutes."""
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Find the uploaded audio file
    from app.config import settings
    meeting_dir = Path(settings.audio_storage_path) / meeting_id
    audio_files = list(meeting_dir.glob("uploaded*"))

    if not audio_files:
        raise HTTPException(status_code=400, detail="No audio file found. Please upload audio first.")

    audio_path = str(audio_files[0])

    # Transcribe audio
    transcript = await asr_provider.transcribe(audio_path)

    # Store transcript
    meeting_store.update_meeting_transcript(meeting_id, [seg.model_dump() for seg in transcript])

    # Generate meeting minutes
    minutes = await llm_provider.generate_minutes(transcript)

    # Store minutes
    meeting_store.update_meeting_minutes(meeting_id, minutes.model_dump())

    return {
        "meeting_id": meeting_id,
        "status": "processed",
        "transcript_segments": len(transcript),
    }


@router.get("/{meeting_id}/transcript")
async def get_transcript(meeting_id: str) -> dict:
    """Get the transcript for a meeting."""
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    transcript = meeting_store.get_meeting_transcript(meeting_id)
    return {"meeting_id": meeting_id, "segments": transcript}


@router.get("/{meeting_id}/minutes")
async def get_minutes(meeting_id: str) -> MeetingMinutes:
    """Get the meeting minutes."""
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    minutes = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes:
        raise HTTPException(status_code=404, detail="Minutes not generated yet. Process the meeting first.")

    return MeetingMinutes(**minutes)


@router.get("/{meeting_id}/audio")
async def get_audio(meeting_id: str):
    """Serve the audio file for a meeting."""
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Find the audio file (uploaded or recorded)
    from app.config import settings
    meeting_dir = Path(settings.audio_storage_path) / meeting_id

    # Check for uploaded file first
    uploaded_files = list(meeting_dir.glob("uploaded*"))
    if uploaded_files:
        return FileResponse(
            uploaded_files[0],
            media_type="audio/mpeg",
            filename=f"meeting_{meeting_id}.mp3"
        )

    # Check for recorded chunks (concatenate them)
    chunk_files = sorted(meeting_dir.glob("*.webm"))
    if chunk_files:
        # For now, just return the first chunk as a sample
        # In production, you'd concatenate all chunks
        return FileResponse(
            chunk_files[0],
            media_type="audio/webm",
            filename=f"meeting_{meeting_id}.webm"
        )

    raise HTTPException(status_code=404, detail="No audio file found")


@router.get("/{meeting_id}/export.docx")
async def export_docx(meeting_id: str):
    """Export meeting minutes as DOCX."""
    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    minutes_data = meeting_store.get_meeting_minutes(meeting_id)
    if not minutes_data:
        raise HTTPException(status_code=404, detail="Minutes not generated yet")

    transcript = meeting_store.get_meeting_transcript(meeting_id)

    from app.models.minutes import MeetingMinutes
    from app.services.export_docx import generate_meeting_docx

    minutes = MeetingMinutes(**minutes_data)
    docx_bytes = generate_meeting_docx(
        meeting_id=meeting_id,
        title=meeting.title,
        created_at=str(meeting.created_at),
        minutes=minutes,
        transcript=transcript,
    )

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=meeting_{meeting_id}.docx"}
    )
