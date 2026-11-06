"""Хэрэглэгчийн толь бичиг — ASR-ийг нэр, нэр томьёог зөв бичихэд чиглүүлж, өмнөх засваруудыг дахин хэрэгжүүлнэ.

Хоёр эх үүсвэрээс бүрдэнэ:
- "Үг засах"-аар хийсэн засвар (wrong → term): ASR-д term-ийг санал болгож, шинэ бичвэрт wrong-ийг автоматаар засна.
- Хэрэглэгчийн гараар нэмсэн үг (зөвхөн term): ASR-д санал болгоно.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import select

from app.db import Meeting, VocabularyTerm, session_scope
from app.models.schemas import VocabularyTerm as VocabularyTermOut
from app.services.text_replace import build_pattern, replace_in_segments, replace_text

# Prompt хэт урт болвол загвар удааширч, санал болгосон үгсийг зохиож бичих эрсдэл нэмэгдэнэ
MAX_HINTS = 80
MAX_HINT_CHARS = 1500
DEFAULT_TITLES = {"шинэ хурал", "new meeting"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _out(t: VocabularyTerm) -> VocabularyTermOut:
    return VocabularyTermOut(id=t.id, term=t.term, wrong=t.wrong)


def list_terms(owner_id: str) -> List[VocabularyTermOut]:
    with session_scope() as s:
        rows = s.scalars(
            select(VocabularyTerm).where(VocabularyTerm.owner_id == owner_id).order_by(VocabularyTerm.updated_at.desc())
        ).all()
        return [_out(t) for t in rows]


def add_term(owner_id: str, term: str, wrong: Optional[str] = None) -> VocabularyTermOut:
    """Үг нэмнэ. Ижил (wrong, term) байвал шинээр үүсгэхгүй, шинэчилсэн огноогоор буцаана."""
    term, wrong = term.strip(), (wrong or "").strip() or None
    if wrong == term:
        wrong = None
    with session_scope() as s:
        rows = s.scalars(select(VocabularyTerm).where(VocabularyTerm.owner_id == owner_id)).all()
        for t in rows:
            if t.term == term and _same(t.wrong, wrong):
                t.updated_at = _now()
                s.flush()
                return _out(t)
        t = VocabularyTerm(owner_id=owner_id, term=term, wrong=wrong, created_at=_now(), updated_at=_now())
        s.add(t)
        s.flush()
        return _out(t)


def remember_correction(owner_id: str, wrong: str, correct: str) -> None:
    """"Үг засах"-аар хийсэн засварыг хадгална.

    - Ижил буруу үгийн хуучин засвар байвал шинэ зөв үгээр солино.
    - Өмнөх засварын зөв үгийг дахин засвал (Бат→Баатар, дараа нь Баатар→Батаа) хуучныг нь шинэ рүү чиглүүлнэ.
    - Эсрэг засвар (Баатар→Бат) өмнөхийг цуцална — хэлбэлзэл үүсгэхгүй.
    """
    wrong, correct = wrong.strip(), correct.strip()
    if not wrong or not correct or wrong == correct:
        return
    with session_scope() as s:
        rows = s.scalars(select(VocabularyTerm).where(VocabularyTerm.owner_id == owner_id)).all()
        handled = False
        for t in rows:
            if t.wrong and _same(t.wrong, correct) and _same(t.term, wrong):
                s.delete(t)  # эсрэг засвар
                handled = True
            elif t.wrong and _same(t.wrong, wrong):
                t.term, t.updated_at = correct, _now()
                handled = True
            elif t.term == wrong:
                t.term, t.updated_at = correct, _now()
        if not handled:
            s.add(VocabularyTerm(owner_id=owner_id, term=correct, wrong=wrong, created_at=_now(), updated_at=_now()))


def delete_term(owner_id: str, term_id: str) -> bool:
    with session_scope() as s:
        t = s.get(VocabularyTerm, term_id)
        if not t or t.owner_id != owner_id:
            return False
        s.delete(t)
        return True


@dataclass
class MeetingVocabulary:
    """Нэг хурлыг боловсруулахад хэрэгтэй толь: ASR-д өгөх үгс ба автомат засварууд."""

    hints: List[str] = field(default_factory=list)
    topic: Optional[str] = None
    corrections: List[Tuple[str, str]] = field(default_factory=list)


def for_meeting(meeting_id: str) -> MeetingVocabulary:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            return MeetingVocabulary()
        rows = s.scalars(
            select(VocabularyTerm).where(VocabularyTerm.owner_id == m.owner_id).order_by(VocabularyTerm.updated_at.desc())
        ).all()
        title = (m.title or "").strip()
        participants = [p for p in (m.participants or []) if isinstance(p, str)]
        terms = [t.term for t in rows]
        corrections = [(t.wrong, t.term) for t in rows if t.wrong]

    hints: List[str] = []
    total = 0
    for h in [*participants, *terms]:  # оролцогчид эхэнд — хамгийн чухал
        h = h.strip()
        if not h or any(_same(h, x) for x in hints):
            continue
        if len(hints) >= MAX_HINTS or total + len(h) > MAX_HINT_CHARS:
            break
        hints.append(h)
        total += len(h) + 2
    topic = title if title and title.casefold() not in DEFAULT_TITLES else None
    # Урт хэллэгийг эхэлж засна ("Бат Эрдэнэ" → ..., дараа нь "Бат" → ...)
    corrections.sort(key=lambda c: len(c[0]), reverse=True)
    return MeetingVocabulary(hints=hints, topic=topic, corrections=corrections)


def _patterns(corrections: Sequence[Tuple[str, str]]):
    return [(build_pattern(w, whole_word=True, case_sensitive=False), c) for w, c in corrections]


def apply_to_segments(segments: list, corrections: Sequence[Tuple[str, str]]) -> Tuple[list, int]:
    total = 0
    for pattern, correct in _patterns(corrections):
        segments, n = replace_in_segments(segments, pattern, correct)
        total += n
    return segments, total


def apply_to_text(text: str, corrections: Sequence[Tuple[str, str]]) -> str:
    for pattern, correct in _patterns(corrections):
        text, _ = replace_text(text, pattern, correct)
    return text
