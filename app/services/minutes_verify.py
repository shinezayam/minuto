"""Минутын нотолгоог тодорхой (LLM-гүй) аргаар шалгана: иш татсан хэсэг нь заасан сегментэд үнэхээр байна уу.

АНХААР: энэ нь "сегмент байна" гэсэн хуурамч батлалт БИШ. Зөвхөн ишлэл (үгчилсэн хэсэг) тэр сегментэд олдож байгааг шалгана.
Ишлэл олдсон ч дүгнэлт зөв гэсэн баталгаа биш — тиймээс UI "иш татсан" гэж бичиж, хүн батлах хүртэл "ноорог" хэвээр байна.
"""
from __future__ import annotations

import re
from typing import Dict, List

from app.models.minutes import MeetingMinutes, TranscriptSegment

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
MIN_QUOTE_CHARS = 6  # хэт богино ишлэл (нэг үг) санамсаргүй таарч болох тул хүлээн авахгүй


def normalize(text: str) -> str:
    """Том/жижиг, тэмдэгт, зайн ялгааг үл тооно (ASR бичвэр ба LLM-ийн ишлэл жижиг ялгаатай байдаг)."""
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").casefold())).strip()


def quote_in_segment(quote: str, segment_text: str) -> bool:
    q = normalize(quote)
    return len(q) >= MIN_QUOTE_CHARS and q in normalize(segment_text)


def check_item(item, segments: Dict[str, str]) -> str:
    """Нэг шийдвэр/даалгаврын нотолгоог шалгаж Verification утга буцаана."""
    if not item.evidence:
        return "no_evidence"
    for ev in item.evidence:
        text = segments.get(ev.segment_id)
        if text is None or not quote_in_segment(ev.quote, text):
            return "quote_not_found"
    return "quote_found"


def verify_minutes(minutes: MeetingMinutes, transcript: List[TranscriptSegment]) -> MeetingMinutes:
    """Шийдвэр, даалгавар бүрт verification тавина. Нотолгоо нь амжилтгүй зүйлийг УСТГАХГҮЙ — тэмдэглээд хүний шалгалтад үлдээнэ.
    Мөн "confirmed" шийдвэр нотолгоогүй бол "unclear" болгож бууруулна (батлагдаагүй зүйлийг батлагдсан мэт харуулахгүй)."""
    segs = {s.id: s.text for s in transcript}
    for item in (*minutes.decisions, *minutes.action_items):
        item.verification = check_item(item, segs)
    for d in minutes.decisions:
        if d.status == "confirmed" and d.verification != "quote_found":
            d.status = "unclear"
    return minutes
