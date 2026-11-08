from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints


class MeetingStatus(str, Enum):
    DRAFT = "draft"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


ParticipantName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class MeetingCreate(BaseModel):
    title: str = Field(default="Шинэ хурал", max_length=200)
    language: str = Field(default="mn", max_length=5)
    participants: List[ParticipantName] = Field(default_factory=list, max_length=50)


class MeetingUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    participants: Optional[List[ParticipantName]] = Field(default=None, max_length=50)


class TranscriptReplace(BaseModel):
    """Бичвэрт нэг үгийг бүх газарт нь солих."""
    find: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    replace: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
    whole_word: bool = True
    case_sensitive: bool = False
    include_minutes: bool = True
    # Засварыг толь бичигт хадгалж, дараагийн хурлуудад автоматаар засна
    remember: bool = True


class TranscriptSegmentUpdate(BaseModel):
    """Нэг сегмент засварлах."""
    id: str
    text: Optional[str] = None
    speaker: Optional[str] = None


class TranscriptUpdate(BaseModel):
    """Бичвэр засварлах - сегментүүд."""
    segments: List[TranscriptSegmentUpdate]


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
    participants: List[str] = Field(default_factory=list)


class MeetingListResponse(BaseModel):
    meetings: List[MeetingResponse]


# ---------- Нэвтрэлт ----------

class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    # Зөвхөн бүртгүүлэхэд
    name: Optional[str] = Field(default=None, max_length=100)


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str] = None


class UserUpdate(BaseModel):
    name: str = Field(max_length=100)


class ContactResponse(BaseModel):
    id: str
    name: str


class ContactListResponse(BaseModel):
    contacts: List[ContactResponse]


VocabText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class VocabularyCreate(BaseModel):
    term: VocabText
    wrong: Optional[VocabText] = None


class VocabularyTerm(BaseModel):
    id: str
    term: str
    wrong: Optional[str] = None


class VocabularyListResponse(BaseModel):
    terms: List[VocabularyTerm]


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
    # "postgresql" | "sqlite" — өгөгдөл сервер дахин эхлэхэд хадгалагдах эсэхийг шалгахад
    database: str = ""
    # Хурлын үеэр бичвэрийг шууд харуулах боломжтой эсэх
    live_transcription: bool = False
    # Тохиргоог шалгахад: Qwen gateway-ийн хост, түлхүүрийн сүүлийн 4 тэмдэгт (түлхүүрийг бүтнээр нь хэзээ ч гаргахгүй)
    qwen_host: str = ""
    qwen_key_hint: str = ""


# ---------- Ask Minuto ----------

class AskMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Байхгүй бол хэрэглэгчийн бүх хурлаас хайна
    meeting_id: Optional[str] = None
    # Шүүлтүүрүүд
    date_from: Optional[str] = None  # YYYY-MM-DD
    date_to: Optional[str] = None  # YYYY-MM-DD
    meeting_ids: Optional[List[str]] = None  # Тодорхой хурлуудаас хайх
    participant: Optional[str] = None  # Оролцогчоор шүүх
    history: List[AskMessage] = Field(default_factory=list, max_length=20)


class AskCitation(BaseModel):
    ref: str
    meeting_id: str
    meeting_title: str
    segment_id: str
    start_ms: int
    end_ms: int
    text: str


class AskResponse(BaseModel):
    # Хариултад эх сурвалжийг [S12] гэж заасан; citations-ийн ref-тэй тохирно
    answer: str
    citations: List[AskCitation]
    meetings_searched: int
