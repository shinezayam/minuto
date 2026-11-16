"""Бичвэрийн сегмент засварлах (цэвэр логик, DB-гүй) ба өөрчлөлтийн шалгалт."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import List, Optional, Tuple


class EditError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.message}"


def etag(transcript: Optional[list]) -> str:
    return hashlib.sha1(json.dumps(transcript or [], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _label(turns: List[dict]) -> bool:
    return any(t.get("speaker") for t in turns)


def _recompute(seg: dict) -> None:
    """turns-ээс сегментийн text, speaker-ийг дахин гаргана ("Нэр: яриа" мөрүүд — apply_speaker_map-тай ижил)."""
    turns = seg.get("turns") or []
    if not turns:
        return
    if _label(turns):
        seg["text"] = "\n".join(f"{t['speaker']}: {t['text']}" if t.get("speaker") else t["text"] for t in turns)
        speakers = {t.get("speaker") for t in turns}
        seg["speaker"] = next(iter(speakers)) if len(speakers) == 1 else None
    else:
        seg["text"] = "\n".join(t["text"] for t in turns)


def apply_edits(transcript: List[dict], edits: List[dict]) -> Tuple[List[dict], List[str]]:
    """
    Бүх засварыг атомаар хэрэглэнэ: аль нэг нь буруу бол юу ч өөрчлөхгүй (EditError).
    Буцаах: (шинэ бичвэр, өөрчлөгдсөн сегментийн id-ууд).

    edit = {id, text?, speaker?, turn_index?, turns?}
      * turns өгвөл — turns-ийг бүхэлд нь солино (text/speaker/turn_index-тэй хамт өгч болохгүй)
      * олон turn-тэй сегментэд text/speaker-ийг turn_index-гүйгээр өгч болохгүй (бүх turn дарагдахаас сэргийлнэ)
      * start_ms/end_ms/id өөрчлөгдөхгүй
    """
    out = copy.deepcopy(transcript)
    by_id = {s.get("id"): s for s in out}
    changed: List[str] = []

    seen: set = set()
    for e in edits:
        sid = e["id"]
        if sid in seen:
            raise EditError("DUPLICATE_SEGMENT", 422, f"Сегмент {sid} нэг хүсэлтэд хоёр удаа байна")
        seen.add(sid)
        seg = by_id.get(sid)
        if seg is None:
            raise EditError("UNKNOWN_SEGMENT", 422, f"Сегмент олдсонгүй: {sid}")

        new_turns = e.get("turns")
        text, speaker, idx = e.get("text"), e.get("speaker"), e.get("turn_index")
        if new_turns is not None:
            if text is not None or speaker is not None or idx is not None:
                raise EditError("AMBIGUOUS_EDIT", 422, "turns өгсөн үед text, speaker, turn_index өгч болохгүй")
            if not new_turns or any(not (t.get("text") or "").strip() for t in new_turns):
                raise EditError("EMPTY_TURN", 422, "turns хоосон эсвэл хоосон текст агуулсан байна")
            seg["turns"] = [{"speaker": (t.get("speaker") or None), "text": t["text"].strip()} for t in new_turns]
            _recompute(seg)
            changed.append(sid)
            continue

        if text is None and speaker is None:
            continue
        if text is not None and not text.strip():
            raise EditError("EMPTY_TEXT", 422, "Текст хоосон байж болохгүй")

        turns = seg.get("turns") or []
        if len(turns) > 1:
            if idx is None:
                raise EditError(
                    "MULTI_TURN_SEGMENT", 422,
                    f"Сегмент {sid} олон хүний ярианаас бүрдсэн: turn_index эсвэл бүтэн turns өгнө үү",
                )
            if not (0 <= idx < len(turns)):
                raise EditError("BAD_TURN_INDEX", 422, f"turn_index хүрээнээс гарсан: {idx}")
            turn = turns[idx]
            if text is not None:
                turn["text"] = text.strip()
            if speaker is not None:
                turn["speaker"] = speaker.strip() or None
            _recompute(seg)
        else:
            if idx not in (None, 0):
                raise EditError("BAD_TURN_INDEX", 422, f"turn_index хүрээнээс гарсан: {idx}")
            if turns:
                if text is not None:
                    turns[0]["text"] = text.strip()
                if speaker is not None:
                    turns[0]["speaker"] = speaker.strip() or None
                _recompute(seg)
                if len(turns) == 1 and not turns[0].get("speaker"):
                    seg["speaker"] = None
            else:
                if text is not None:
                    seg["text"] = text.strip()
                if speaker is not None:
                    seg["speaker"] = speaker.strip() or None
        changed.append(sid)
    return out, changed
