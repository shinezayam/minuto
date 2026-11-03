from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


class MeetingStatus(str, Enum):
    DRAFT = "draft"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class MeetingCreate(BaseModel):
    title: str = Field(default="Шинэ хурал", max_length=200)
    language: str = Field(default="mn", max_length=5)


class MeetingUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)


class MeetingResponse(BaseModel):
    id: str
    title: str
    status: MeetingStatus
    language: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: int = 0
    created_at: datetime
    updated_at: datetime
    # Ард боловсруулах явц: queued → transcribing (done/total хэсэг) → summarizing → done | failed
    processing_stage: Optional[str] = None
    processing_done: int = 0
    processing_total: int = 0
    # "CODE: тайлбар" хэлбэртэй (NO_AUDIO, NO_SPEECH, ASR_FAILED, SUMMARY_FAILED, INTERRUPTED, INTERNAL_ERROR)
    processing_error: Optional[str] = None


class MeetingListResponse(BaseModel):
    meetings: List[MeetingResponse]


# ---------- Нэвтрэлт ----------

class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)


class UserResponse(BaseModel):
    id: str
    email: str


class AuthResponse(BaseModel):
    token: str
    user: UserResponse


class AudioChunkMeta(BaseModel):
    meeting_id: str
    sequence: int
    mime_type: str
    timestamp_ms: int
    size_bytes: int
    duration_ms: Optional[int] = None


class WSAudioMessage(BaseModel):
    """Metadata frame sent before binary audio chunk."""
    type: str = "audio.chunk"
    meeting_id: str
    sequence: int
    mime_type: str
    timestamp_ms: int


class WSAck(BaseModel):
    type: str = "audio.ack"
    sequence: int
    meeting_id: str


class WSError(BaseModel):
    type: str = "error"
    code: str
    message: str


class WSRecordingReady(BaseModel):
    type: str = "recording.ready"
    meeting_id: str


class WSProcessingStatus(BaseModel):
    type: str = "processing.status"
    stage: str
    queued_chunks: int = 0


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    asr_provider: str
    llm_provider: str = "demo"
