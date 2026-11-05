from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel


class SpeakerTurn(BaseModel):
    """Сегмент доторх нэг хүний яриа."""
    speaker: Optional[str] = None
    text: str


class TranscriptSegment(BaseModel):
    id: str
    start_ms: int
    end_ms: int
    text: str
    # Сегментэд ганц хүн ярьсан бол түүний нэр/шошго
    speaker: Optional[str] = None
    turns: List[SpeakerTurn] = []


class Topic(BaseModel):
    title: str
    description: str = ""
    source_segment_ids: List[str] = []


class Decision(BaseModel):
    text: str
    rationale: Optional[str] = None
    source_segment_ids: List[str] = []


class ActionItem(BaseModel):
    task: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "open"
    source_segment_ids: List[str] = []


class OpenQuestion(BaseModel):
    text: str
    source_segment_ids: List[str] = []


class QAPair(BaseModel):
    question: str
    asked_by: Optional[str] = None
    answer: Optional[str] = None  # хариулаагүй бол null
    answered_by: Optional[str] = None
    source_segment_ids: List[str] = []


class MeetingMinutes(BaseModel):
    title: str
    summary: str
    topics: List[Topic] = []
    decisions: List[Decision] = []
    action_items: List[ActionItem] = []
    open_questions: List[OpenQuestion] = []
    # Ярьсан хүмүүс (нэр нь тодорхой бол нэрээр, үгүй бол "Яригч 1", "Яригч 2")
    participants: List[str] = []
    qa: List[QAPair] = []
    # "Minuto / <хөтлөгчийн нэр>" — хадгалахгүй, уншихад нэмнэ
    prepared_by: Optional[str] = None
    # LLM-ийн гаргасан {сегмент: {сегмент доторх шошго: хурлын хэмжээний яригч}}; бичвэрт хэрэглээд хоосолно
    speaker_map: Dict[str, Dict[str, str]] = {}
