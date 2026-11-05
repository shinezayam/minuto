"""
Хурлын бичлэгийг ард нь боловсруулна: аудио → бичвэр (ASR) → хурлын бичиг (LLM).
/process шууд хариу өгч, явцыг meetings хүснэгтэд бичнэ; frontend түүнийг асууж харуулна.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Set

from app.config import settings
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.services import meeting_store
from app.services.asr import ProviderError
from app.services.audio_utils import find_meeting_audio
from app.services.speakers import apply_speaker_map

logger = logging.getLogger(__name__)

# Ажиллаж буй task-уудыг хадгална (хадгалахгүй бол garbage collector устгаж болно)
_tasks: Set[asyncio.Task] = set()


def meeting_dir(meeting_id: str) -> Path:
    return Path(settings.audio_storage_path) / meeting_id


def has_audio(meeting_id: str) -> bool:
    d = meeting_dir(meeting_id)
    return d.is_dir() and any(p.name.startswith("uploaded") or p.name[:6].isdigit() for p in d.iterdir())


async def run(meeting_id: str) -> None:
    stage = "transcribing"
    try:
        audio = await asyncio.to_thread(find_meeting_audio, meeting_dir(meeting_id))
        if not audio:
            meeting_store.finish_processing(meeting_id, "NO_AUDIO: Аудио файл олдсонгүй")
            return

        meeting_store.set_progress(meeting_id, "transcribing", 0, 0)
        transcript = await asr_mod.asr_provider.transcribe(
            str(audio),
            on_progress=lambda done, total: meeting_store.set_progress(meeting_id, "transcribing", done, total),
        )
        # Тэмдэглэл гаргахаас өмнө хадгална — LLM алдаа гарсан ч бичвэр (хэсэг доторх яригчтай) алга болохгүй
        transcript = apply_speaker_map(transcript, {})
        meeting_store.update_meeting_transcript(meeting_id, [s.model_dump() for s in transcript])
        if not transcript:
            meeting_store.finish_processing(meeting_id, "NO_SPEECH: Бичлэгээс яриа танигдсангүй")
            return

        stage = "summarizing"
        meeting_store.set_progress(meeting_id, "summarizing")
        participants = meeting_store.get_meeting_context(meeting_id)["participants"]
        minutes = await llm_mod.llm_provider.generate_minutes(transcript, participants)
        # Хэсэг бүрийн "Яригч N"-ийг хурлын хэмжээний яригч болгож бичвэрийг шинэчилнэ
        transcript = apply_speaker_map(transcript, minutes.speaker_map)
        meeting_store.update_meeting_transcript(meeting_id, [s.model_dump() for s in transcript])
        minutes.speaker_map = {}
        meeting_store.update_meeting_minutes(meeting_id, minutes.model_dump(exclude={"prepared_by"}))
        meeting_store.finish_processing(meeting_id)
        logger.info("Meeting %s processed: %d segments", meeting_id, len(transcript))

    except (ProviderError, ValueError) as e:
        code = "SUMMARY_FAILED" if stage == "summarizing" else "ASR_FAILED"
        logger.warning("Meeting %s %s: %s", meeting_id, code, e)
        meeting_store.finish_processing(meeting_id, f"{code}: {e}")
    except Exception as e:  # noqa: BLE001 — ямар ч алдаа "processing" төлөвт гацаахгүй
        logger.exception("Meeting %s processing crashed", meeting_id)
        meeting_store.finish_processing(meeting_id, f"INTERNAL_ERROR: {type(e).__name__}")


def start(meeting_id: str) -> asyncio.Task:
    task = asyncio.create_task(run(meeting_id), name=f"process:{meeting_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def wait_all() -> None:
    """Тестэд: ажиллаж буй бүх боловсруулалт дуустал хүлээнэ."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
