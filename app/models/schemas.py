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


class MeetingListResponse(BaseModel):
    meetings: List[MeetingResponse]


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
