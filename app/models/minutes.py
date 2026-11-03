from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel


class TranscriptSegment(BaseModel):
    id: str
    start_ms: int
    end_ms: int
    text: str
    speaker: Optional[str] = None


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


class MeetingMinutes(BaseModel):
    title: str
    summary: str
    topics: List[Topic] = []
    decisions: List[Decision] = []
    action_items: List[ActionItem] = []
    open_questions: List[OpenQuestion] = []
