"""Бичвэр, тэмдэглэлд нэг үгийг бүх газарт нь солих (буруу танигдсан нэр, нэр томьёог засахад)."""
from __future__ import annotations

import re
from typing import Any, Tuple

# Эдгээр түлхүүрийн утга нь ID/техникийн утга тул солихгүй
_SKIP_KEYS = {"id", "source_segment_ids", "segment_id", "start_ms", "end_ms", "speaker_map"}


def build_pattern(find: str, whole_word: bool, case_sensitive: bool) -> re.Pattern[str]:
    """Хайх үгийн regex. whole_word үед үсэг/тоонд наалдсан хэсгийг (жишээ нь "Бат" → "Батын") алгасна."""
    body = re.escape(find)
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"  # Python-ий \w кирилл үсгийг таньдаг
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def replace_text(text: str, pattern: re.Pattern[str], replacement: str) -> Tuple[str, int]:
    # lambda: replacement доторх "\1" зэргийг regex-ийн лавлагаа гэж ойлгуулахгүй
    return pattern.subn(lambda _m: replacement, text)


def replace_in_segments(segments: list, pattern: re.Pattern[str], replacement: str) -> Tuple[list, int]:
    """Хэсгийн text, speaker болон turns доторх text, speaker-т солино.

    Тоолохдоо дэлгэцэнд харагдах текстийг тоолно: turns байвал тэдгээрийн text (segment.text-д
    "Яригч: " шошго орсон байдаг), үгүй бол segment.text.
    """
    total = 0
    out = []
    for seg in segments:
        seg = dict(seg)
        seg["text"], n_text = replace_text(seg.get("text") or "", pattern, replacement)
        if seg.get("speaker"):
            seg["speaker"], _ = replace_text(seg["speaker"], pattern, replacement)
        turns = []
        n_turns = 0
        for turn in seg.get("turns") or []:
            turn = dict(turn)
            turn["text"], n = replace_text(turn.get("text") or "", pattern, replacement)
            n_turns += n
            if turn.get("speaker"):
                turn["speaker"], _ = replace_text(turn["speaker"], pattern, replacement)
            turns.append(turn)
        seg["turns"] = turns
        total += n_turns if turns else n_text
        out.append(seg)
    return out, total


def replace_in_obj(obj: Any, pattern: re.Pattern[str], replacement: str) -> Tuple[Any, int]:
    """Тэмдэглэлийн (minutes) бүх текст утгад рекурсив солино."""
    if isinstance(obj, str):
        return replace_text(obj, pattern, replacement)
    if isinstance(obj, list):
        total = 0
        items = []
        for item in obj:
            item, n = replace_in_obj(item, pattern, replacement)
            items.append(item)
            total += n
        return items, total
    if isinstance(obj, dict):
        total = 0
        result = {}
        for key, value in obj.items():
            if key in _SKIP_KEYS:
                result[key] = value
                continue
            result[key], n = replace_in_obj(value, pattern, replacement)
            total += n
        return result, total
    return obj, 0
