"""
Бичвэрийг сэдвүүдэд хуваана.
LLM ашиглан бичвэрийн сегментүүдийг бүлэглэнэ.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

import httpx

from app.config import settings
from app.models.minutes import TranscriptSegment
from app.services.llm import ProviderError

logger = logging.getLogger(__name__)

# Tokenhub API endpoint
TOKENHUB_URL = "https://tokenhub.mn/v1/chat/completions"


class TopicSegment:
    """Сэдвийн хэсэг"""
    def __init__(
        self,
        title: str,
        description: str,
        start_ms: int,
        end_ms: int,
        segment_ids: List[str],
    ):
        self.title = title
        self.description = description
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.segment_ids = segment_ids


def _format_timestamp(ms: int) -> str:
    """Миллисекундыг MM:SS хэлбэрт хөрвүүлнэ"""
    s = ms // 1000
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


def _build_prompt(transcript: List[TranscriptSegment]) -> str:
    """LLM-д илгээх prompt"""
    lines = []
    for seg in transcript:
        ts = _format_timestamp(seg.start_ms)
        speaker = seg.speaker or "Яригч"
        text = seg.text.strip()
        lines.append(f"[{ts}] {speaker}: {text}")
    
    transcript_text = "\n".join(lines)
    
    return f"""Та хурлын бичвэрийг сэдвүүдэд хуваах даалгавартай.

Бичвэр:
{transcript_text}

Дээрх бичвэрийг утга агуулгаар нь сэдвүүдэд хуваана уу. Хүснэгтэн хэлбэрээр хариулна уу:

Формат:
1. Сэдвийн гарчиг | Товч тайлбар | Эхлэх цаг (MM:SS) | Дуусах цаг (MM:SS) | Сегментийн дугаарууд (зайгаар тусгаарласан)

Жишээ:
1. Танилцуулга | Хурлын зорилго, оролцогчид | 00:00 | 02:30 | 1 2 3
2. Маркетингийн төлөвлөгөө | Шинэ кампанит ажлын тухай | 02:30 | 15:45 | 4 5 6 7 8

Зөвхөн сэдвүүдийг жагсаана уу. Бусад тайлбар бичихгүй."""


async def segment_topics(transcript: List[TranscriptSegment]) -> List[TopicSegment]:
    """Бичвэрийг сэдвүүдэд хуваана"""
    if not transcript:
        return []
    
    # Богино бичвэрийг нэг сэдэв гэж үзнэ
    if len(transcript) < 5:
        return [
            TopicSegment(
                title="Хурал",
                description="Богино хурал",
                start_ms=transcript[0].start_ms,
                end_ms=transcript[-1].end_ms,
                segment_ids=[s.id for s in transcript],
            )
        ]
    
    prompt = _build_prompt(transcript)
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                TOKENHUB_URL,
                headers={
                    "Authorization": f"Bearer {settings.qwen_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.qwen_model,
                    "messages": [
                        {"role": "system", "content": "Та хурлын бичвэрийг сэдвүүдэд хуваах мэргэжилтэн."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            
            # Parse response
            topics = _parse_topics(content, transcript)
            return topics
            
    except httpx.HTTPError as e:
        logger.warning("Topic segmentation failed: %s", e)
        raise ProviderError(f"Topic segmentation failed: {e}") from e


def _parse_topics(response: str, transcript: List[TranscriptSegment]) -> List[TopicSegment]:
    """LLM-ийн хариултыг parse хийнэ"""
    topics = []
    
    # Segment ID mapping
    seg_by_num = {i + 1: seg for i, seg in enumerate(transcript)}
    
    # Parse numbered lines
    for line in response.split("\n"):
        line = line.strip()
        if not line or not re.match(r"^\d+\.", line):
            continue
        
        # Remove number prefix
        line = re.sub(r"^\d+\.\s*", "", line)
        
        # Split by |
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        
        title = parts[0]
        description = parts[1]
        
        # Parse timestamps
        try:
            start_parts = parts[2].split(":")
            start_ms = (int(start_parts[0]) * 60 + int(start_parts[1])) * 1000
            
            end_parts = parts[3].split(":")
            end_ms = (int(end_parts[0]) * 60 + int(end_parts[1])) * 1000
        except (ValueError, IndexError):
            continue
        
        # Parse segment IDs
        try:
            seg_nums = [int(n) for n in parts[4].split()]
            seg_ids = [seg_by_num[n].id for n in seg_nums if n in seg_by_num]
        except (ValueError, IndexError):
            seg_ids = []
        
        if title and seg_ids:
            topics.append(TopicSegment(
                title=title,
                description=description,
                start_ms=start_ms,
                end_ms=end_ms,
                segment_ids=seg_ids,
            ))
    
    # Хэрэв parse хийж чадаагүй бол бүгдийг нэг сэдэв гэж үзнэ
    if not topics:
        topics = [
            TopicSegment(
                title="Хурал",
                description="Хурлын бичвэр",
                start_ms=transcript[0].start_ms,
                end_ms=transcript[-1].end_ms,
                segment_ids=[s.id for s in transcript],
            )
        ]
    
    return topics
