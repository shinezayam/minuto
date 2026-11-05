"""
Яригчийг ялгах.

Аудиог 30 секундын хэсгүүдэд хувааж зэрэг илгээдэг тул Qwen-Omni хэсэг бүрт яригчийг
"Яригч 1:", "Яригч 2:" гэж зөвхөн тэр хэсэг дотор нь дугаарлана. Хурлын тэмдэглэл гаргах LLM
бүтэн бичвэрийг уншаад эдгээр шошгыг хурлын хэмжээнд нэгтгэнэ (speaker_map); энд түүнийг хэрэглэнэ.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from app.models.minutes import SpeakerTurn, TranscriptSegment

SPEAKER_PREFIX = "Яригч"

# "Яригч 1: ...", "**Яригч 2:** ...", "Speaker 1: ..." эсвэл Omni өөрөө өгсөн "Дарга: ..." (1-2 үг)
_LABEL_RE = re.compile(
    r"^\s*\**\s*(?P<label>(?:Яригч|Speaker|Ярьж буй хүн)\s*\d+|[А-ЯӨҮЁA-Z][^\s:：*\d]{0,20}(?:\s[^\s:：*\d]{1,20})?)"
    r"\s*\**\s*[:：]\s*\**\s*(?P<text>.*)$"
)
_NUM_RE = re.compile(r"(?:Яригч|Speaker|Ярьж буй хүн)\s*(\d+)$")


def normalize_label(label: str) -> str:
    """"Speaker 2" → "Яригч 2"; бусдыг хэвээр."""
    label = " ".join(label.split())
    m = _NUM_RE.match(label)
    return f"{SPEAKER_PREFIX} {m.group(1)}" if m else label


def parse_turns(text: str) -> List[Tuple[Optional[str], str]]:
    """Сегментийн бичвэрийг (шошго, яриа) хэсгүүдэд задална. Шошгогүй мөр өмнөх хүний ярианд нэмэгдэнэ."""
    turns: List[Tuple[Optional[str], str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LABEL_RE.match(line)
        if m and m.group("text").strip():
            turns.append((normalize_label(m.group("label")), m.group("text").strip()))
        elif turns:
            label, prev = turns[-1]
            turns[-1] = (label, f"{prev} {line}")
        else:
            turns.append((None, line))
    return turns


def _merge_adjacent(turns: List[SpeakerTurn]) -> List[SpeakerTurn]:
    out: List[SpeakerTurn] = []
    for t in turns:
        if out and out[-1].speaker == t.speaker:
            out[-1] = SpeakerTurn(speaker=t.speaker, text=f"{out[-1].text} {t.text}")
        else:
            out.append(t)
    return out


def apply_speaker_map(
    segments: List[TranscriptSegment], speaker_map: Dict[str, Dict[str, str]]
) -> List[TranscriptSegment]:
    """
    Сегмент бүрийн дотоод шошгыг хурлын хэмжээний яригч болгоно, turns-ийг бөглөж,
    бичвэрийг "Нэр: яриа" мөрүүдээр дахин бичнэ. Map байхгүй шошго хэвээр үлдэнэ.
    """
    out: List[TranscriptSegment] = []
    for seg in segments:
        local_map = {normalize_label(k): v.strip() for k, v in (speaker_map.get(seg.id) or {}).items() if v and v.strip()}
        turns = _merge_adjacent(
            [SpeakerTurn(speaker=local_map.get(label, label) if label else None, text=t) for label, t in parse_turns(seg.text)]
        )
        speakers = {t.speaker for t in turns}
        labeled = any(t.speaker for t in turns)
        text = "\n".join(f"{t.speaker}: {t.text}" if t.speaker else t.text for t in turns) if labeled else seg.text
        out.append(
            seg.model_copy(
                update={
                    "turns": turns if labeled else [],
                    "speaker": next(iter(speakers)) if labeled and len(speakers) == 1 else None,
                    "text": text,
                }
            )
        )
    return out
