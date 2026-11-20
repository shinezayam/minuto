from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, computed_field

# Шийдвэрийн төлөв: зөвхөн оролцогчид ил тод зөвшөөрсөн бол "confirmed". Санал = "proposed", эргэлзээтэй/муу сонсогдсон = "unclear".
DecisionStatus = Literal["proposed", "confirmed", "rejected", "unclear"]
# Баримтын шалгалт (тодорхой, LLM-гүй): "quote_found" = иш татсан хэсэг өөрөө бичвэрт үнэхээр байна;
# "quote_not_found" = ишлэл бичвэрт олдсонгүй; "no_evidence" = ишлэл өгөөгүй (зөвхөн сегментийн id-тай). Эдгээрийн аль нь ч
# "логикийн хувьд батлагдсан" гэсэн үг БИШ — зөвхөн иш татсан эх хэсэг бодитой гэдгийг л хэлнэ.
Verification = Literal["quote_found", "quote_not_found", "no_evidence"]
ReviewDecision = Literal["accepted", "edited", "rejected"]
MinutesStatus = Literal["draft", "in_review", "approved"]


class Evidence(BaseModel):
    """Нотолгоо: яг ямар сегментээс, ямар үгчилсэн хэсгийг иш татсан."""
    segment_id: str
    quote: str


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
    # Хуучин минутад байхгүй → "unclear" (үнэлэгдээгүй). Шинэ минутад LLM зааж өгнө.
    status: DecisionStatus = "unclear"
    # Загварын өөрийн үнэлгээ (0..1); баталгаа БИШ
    confidence: Optional[float] = None
    evidence: List[Evidence] = []
    verification: Verification = "no_evidence"
    # Хүний шалгалт: хэрэглэгч хүлээн авсан / засварласан / татгалзсан
    review: Optional[ReviewDecision] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_review(self) -> bool:
        """Хүний анхаарал шаардлагатай: ишлэл батлагдаагүй, эсвэл санал/эргэлзээтэй. Хүн хүлээн авсан бол шаардахгүй."""
        if self.review in ("accepted", "edited", "rejected"):
            return False
        return self.verification != "quote_found" or self.status in ("unclear", "proposed")


class ActionItem(BaseModel):
    task: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "open"
    source_segment_ids: List[str] = []
    confidence: Optional[float] = None
    evidence: List[Evidence] = []
    verification: Verification = "no_evidence"
    review: Optional[ReviewDecision] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_review(self) -> bool:
        if self.review in ("accepted", "edited", "rejected"):
            return False
        return self.verification != "quote_found"


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
    # Хүний батлалт: AI-ийн гаргасан бүх минут "draft"; хүн "approved" болгосноор л батлагдсан гэж үзнэ
    minutes_status: MinutesStatus = "draft"
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    # LLM-ийн гаргасан {сегмент: {сегмент доторх шошго: хурлын хэмжээний яригч}}; бичвэрт хэрэглээд хоосолно
    speaker_map: Dict[str, Dict[str, str]] = {}
    # Загвараар эхэлсэн хурлын онцлох хэсгүүд (topics/decisions/action_items/open_questions); UI эдгээрийг дээр нь харуулна
    section_order: List[str] = []
