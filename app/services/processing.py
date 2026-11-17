"""
Хурлын бичлэгийг ард нь боловсруулна: аудио → бичвэр (ASR) → хурлын бичиг (LLM).
/process шууд хариу өгч, явцыг meetings хүснэгтэд бичнэ; frontend түүнийг асууж харуулна.
"""
from __future__ import annotations

import asyncio
import errno
import logging
from pathlib import Path
from typing import Set

from app.config import settings
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.models.minutes import TranscriptSegment
from app.services import meeting_store, vocabulary
from app.services.asr import ProviderError
from app.services import audio_store, jobs
from app.services.audio_utils import find_meeting_audio
from app.services.speakers import apply_speaker_map

logger = logging.getLogger(__name__)

def meeting_dir(meeting_id: str) -> Path:
    return Path(settings.audio_storage_path) / meeting_id


def has_audio(meeting_id: str) -> bool:
    d = meeting_dir(meeting_id)
    return d.is_dir() and any(p.name.startswith("uploaded") or p.name[:6].isdigit() for p in d.iterdir())


async def run(meeting_id: str, job=None) -> bool:
    """Боловсруулалтыг ажиллуулна. Амжилттай бол True. job өгвөл ASR хэсгүүдийг хадгалж, provider-ийн түр алдаанд RetryLater шиднэ."""
    stage = "transcribing"
    try:
        await asyncio.to_thread(audio_store.ensure_local, meeting_id)  # restart/deploy-оор диск арилсан бол remote-оос сэргээнэ
        audio = await asyncio.to_thread(find_meeting_audio, meeting_dir(meeting_id))
        if not audio:
            meeting_store.finish_processing(meeting_id, "NO_AUDIO: Аудио файл олдсонгүй")
            return False

        meeting_store.set_progress(meeting_id, "transcribing", 0, 0)
        vocab = await asyncio.to_thread(vocabulary.for_meeting, meeting_id)
        transcript = await asr_mod.asr_provider.transcribe(
            str(audio),
            on_progress=lambda done, total: meeting_store.set_progress(meeting_id, "transcribing", done, total),
            context=asr_mod.ASRContext(
                hints=vocab.hints, topic=vocab.topic,
                resume=dict(job.chunks) if job else {}, on_chunk=job.save_chunk if job else None,
            ),
        )
        transcript = apply_speaker_map(transcript, {})
        if vocab.corrections:
            # Хэрэглэгчийн өмнө нь хийсэн засваруудыг ("Үг засах") шинэ бичвэрт хэрэгжүүлнэ
            fixed, n = vocabulary.apply_to_segments([s.model_dump() for s in transcript], vocab.corrections)
            if n:
                logger.info("Meeting %s: applied %d vocabulary corrections", meeting_id, n)
                transcript = [TranscriptSegment(**s) for s in fixed]
        # Тэмдэглэл гаргахаас өмнө хадгална — LLM алдаа гарсан ч бичвэр (хэсэг доторх яригчтай) алга болохгүй
        meeting_store.update_meeting_transcript(meeting_id, [s.model_dump() for s in transcript], snapshot_kind="reprocess")
        if not transcript:
            meeting_store.finish_processing(meeting_id, "NO_SPEECH: Бичлэгээс яриа танигдсангүй")
            return False

        stage = "summarizing"
        meeting_store.set_progress(meeting_id, "summarizing")
        ctx = meeting_store.get_meeting_context(meeting_id)
        minutes = await llm_mod.llm_provider.generate_minutes(
            transcript, ctx["participants"], template=ctx["template"], language=ctx["language"],
            notes=ctx.get("notes") or None,
        )
        # Хэсэг бүрийн "Яригч N"-ийг хурлын хэмжээний яригч болгож бичвэрийг шинэчилнэ
        transcript = apply_speaker_map(transcript, minutes.speaker_map)
        meeting_store.update_meeting_transcript(meeting_id, [s.model_dump() for s in transcript])
        minutes.speaker_map = {}
        meeting_store.update_meeting_minutes(meeting_id, minutes.model_dump(exclude={"prepared_by"}))
        meeting_store.finish_processing(meeting_id)
        logger.info("Meeting %s processed: %d segments", meeting_id, len(transcript))
        return True

    except (ProviderError, ValueError) as e:
        code = "SUMMARY_FAILED" if stage == "summarizing" else "ASR_FAILED"
        logger.warning("Meeting %s %s: %s", meeting_id, code, e)  # дэлгэрэнгүй (provider-ийн текст) зөвхөн лог руу
        if job is not None and isinstance(e, ProviderError) and job.can_retry():
            # Түр алдаа (сүлжээ, provider): дахин оролдоно. Meeting "processing" хэвээр, шат нь "retrying"
            meeting_store.set_progress(meeting_id, "retrying")
            raise jobs.RetryLater(f"{code}: {type(e).__name__}") from e
        if isinstance(e, ProviderError):
            msg = "Хиймэл оюуны үйлчилгээ түр ажиллахгүй байна. Түр хүлээгээд дахин оролдоно уу."
        else:
            msg = str(e)[:200]  # ValueError-ууд манай өөрийн, хэрэглэгчид зориулсан мессеж
        meeting_store.finish_processing(meeting_id, f"{code}: {msg}")
        return False
    except OSError as e:
        if e.errno != errno.ENOSPC:
            logger.exception("Meeting %s processing crashed (OSError)", meeting_id)
            meeting_store.finish_processing(meeting_id, f"INTERNAL_ERROR: {type(e).__name__}")
            return False
        # Диск дүүрсэн: түр файлууд аль хэдийн устсан. Зай чөлөөлөгдөх боломжтой тул дахин оролдоно
        logger.error("Meeting %s: disk full during processing", meeting_id)
        if job is not None and job.can_retry():
            meeting_store.set_progress(meeting_id, "retrying")
            raise jobs.RetryLater("DISK_FULL") from e
        meeting_store.finish_processing(meeting_id, "DISK_FULL: Серверийн диск дүүрсэн тул боловсруулж чадсангүй. Дараа дахин оролдоно уу.")
        return False
    except Exception as e:  # noqa: BLE001 — ямар ч алдаа "processing" төлөвт гацаахгүй
        logger.exception("Meeting %s processing crashed", meeting_id)
        meeting_store.finish_processing(meeting_id, f"INTERNAL_ERROR: {type(e).__name__}")
        return False


def start(meeting_id: str) -> str:
    """Боловсруулалтын ажил үүсгэнэ (DB-д хадгалагдана) ба worker-уудыг ажиллуулна. Давхар бол jobs.AlreadyActive."""
    job_id = jobs.enqueue(meeting_id)
    jobs.ensure_workers()
    return job_id


async def wait_all() -> None:
    """Тестэд / graceful shutdown: бүх ажил дуустал хүлээж, worker-уудыг зогсооно."""
    await jobs.wait_idle()
    await jobs.shutdown()
