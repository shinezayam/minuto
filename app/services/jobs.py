"""Боловсруулалтын durable ажлын дараалал (процесс дотор ажиллах worker-тэй).

* enqueue()         — ажил үүсгэнэ; нэг хуралд зэрэг ганц идэвхтэй ажил (DB-ийн unique guard) → давхар POST /process нэг ажил
* claim_next()      — queued, эсвэл lease нь дууссан (унасан) running ажлыг атомаар авна; оролдлогыг нэмнэ
* heartbeat         — ажиллаж байхад lease сунгана; тасарвал өөр worker (эсвэл дахин эхэлсний дараа) авна
* дууссан ASR хэсгүүд DB-д хадгалагдана → үргэлжлүүлэхэд дахин төлбөр төлж, дахин хийхгүй
* RetryLater         — зөвхөн provider-ийн түр алдаанд; backoff-тэй; max_attempts-д хүрвэл эцсийн алдаа
Нэг instance дээр ажиллахаар зохиосон (documented): олон instance бол claim нь optimistic атомар UPDATE тул аюулгүй, харин
recover_on_startup() өөр instance-ийн ажлыг булаана.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import Meeting, ProcessingJob, session_scope

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AlreadyActive(Exception):
    """Энэ хуралд идэвхтэй (queued/running) ажил аль хэдийн байна."""


class RetryLater(Exception):
    """Provider-ийн түр алдаа: ажлыг backoff-ийн дараа дахин оролдоно."""


@dataclass
class JobCtx:
    job_id: str
    meeting_id: str
    attempts: int
    max_attempts: int
    chunks: Dict[int, dict] = field(default_factory=dict)

    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts

    def save_chunk(self, index: int, data: dict) -> None:
        """Нэг ASR хэсэг дуусах бүрд DB-д хадгална (үргэлжлүүлэхэд ашиглана)."""
        self.chunks[index] = data
        with session_scope() as s:
            s.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == self.job_id)
                .values(chunks={str(k): v for k, v in self.chunks.items()}, updated_at=_now())
            )


def enqueue(meeting_id: str) -> str:
    try:
        with session_scope() as s:
            job = ProcessingJob(meeting_id=meeting_id, max_attempts=settings.job_max_attempts, run_after=_now())
            s.add(job)
            s.flush()
            return job.id
    except IntegrityError as e:
        raise AlreadyActive(meeting_id) from e


def has_active(meeting_id: str) -> bool:
    with session_scope() as s:
        return s.scalar(
            select(ProcessingJob.id).where(ProcessingJob.meeting_id == meeting_id, ProcessingJob.state.in_(("queued", "running")))
        ) is not None


def claim_next() -> Optional[JobCtx]:
    """Атомар: candidate-ийг олж, нөхцөлтэй UPDATE-ээр авна (rowcount==1 бол бидний). SQLite/Postgres хоёуланд ажиллана."""
    now = _now()
    with session_scope() as s:
        cand = s.scalars(
            select(ProcessingJob)
            .where(
                or_(
                    and_(ProcessingJob.state == "queued", ProcessingJob.run_after <= now),
                    and_(ProcessingJob.state == "running", ProcessingJob.lease_expires_at < now),  # унасан ажил
                )
            )
            .order_by(ProcessingJob.created_at)
            .limit(5)
        ).all()
        for job in cand:
            was_running = job.state == "running"
            if was_running and job.attempts >= job.max_attempts:
                # Унасаар л байгаа "poison" ажил: дахин оролдохгүй
                _fail_locked(s, job, "INTERRUPTED: Боловсруулалт удаа дараа тасарсан. Дахин оролдоно уу.")
                continue
            # ORM UPDATE нь session доторх job объектыг ч шинэчилдэг тул ӨМНӨХ утгуудыг эхлээд хадгална
            prev_state, prev_attempts, job_id, meeting_id, max_attempts = job.state, job.attempts, job.id, job.meeting_id, job.max_attempts
            chunks = {int(k): v for k, v in (job.chunks or {}).items()}
            res = s.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.id == job_id,
                    ProcessingJob.state == prev_state,
                    ProcessingJob.attempts == prev_attempts,  # өөр worker аль хэдийн авсан бол таарахгүй
                )
                .values(
                    state="running",
                    attempts=prev_attempts + 1,
                    lease_expires_at=now + timedelta(seconds=settings.job_lease_sec),
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            if res.rowcount == 1:
                logger.info("Job %s claimed (meeting %s, attempt %d%s)", job_id, meeting_id, prev_attempts + 1, ", recovered" if was_running else "")
                return JobCtx(job_id=job_id, meeting_id=meeting_id, attempts=prev_attempts + 1, max_attempts=max_attempts, chunks=chunks)
    return None


def _fail_locked(s, job: ProcessingJob, error: str) -> None:
    """Ажил болон хурлыг НЭГ session/transaction-д алдаатай болгоно (SQLite-д хоёр дахь холболт түгжигдэнэ)."""
    job.state, job.last_error, job.updated_at = "failed", error, _now()
    s.execute(
        update(Meeting)
        .where(Meeting.id == job.meeting_id)
        .values(status="failed", processing_stage="failed", processing_error=error, updated_at=_now())
    )


def heartbeat(job_id: str) -> None:
    now = _now()
    with session_scope() as s:
        s.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.state == "running")
            .values(lease_expires_at=now + timedelta(seconds=settings.job_lease_sec), heartbeat_at=now)
        )


def complete(job_id: str, ok: bool, error: Optional[str] = None) -> None:
    with session_scope() as s:
        s.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(state="succeeded" if ok else "failed", last_error=error, lease_expires_at=None, updated_at=_now())
        )


def requeue(job_id: str, error: str, attempts: int) -> float:
    """Түр алдааны дараа backoff-той дахин дараалалд. Буцаах: хүлээх секунд."""
    delay = min(settings.job_backoff_sec * 2 ** (attempts - 1), 120)
    with session_scope() as s:
        s.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(state="queued", last_error=error, run_after=_now() + timedelta(seconds=delay), lease_expires_at=None, updated_at=_now())
        )
    return delay


def recover_on_startup() -> dict:
    """Дахин эхлэхэд: running ажлуудын lease-ийг дуусгаж (шууд үргэлжлүүлнэ), ажилгүй "processing" хурлуудыг алдаатай болгоно."""
    from app.services import meeting_store

    with session_scope() as s:
        n = s.execute(
            update(ProcessingJob).where(ProcessingJob.state == "running").values(lease_expires_at=_now() - timedelta(seconds=1))
        ).rowcount
    orphans = meeting_store.fail_interrupted_processing()
    if n or orphans:
        logger.warning("Startup recovery: %d interrupted job(s) re-queued, %d orphan meeting(s) failed", n, orphans)
    return {"requeued": n, "orphans": orphans}


def list_jobs(meeting_id: str) -> List[dict]:
    with session_scope() as s:
        rows = s.scalars(select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id).order_by(ProcessingJob.created_at)).all()
        return [
            {"id": r.id, "state": r.state, "attempts": r.attempts, "max_attempts": r.max_attempts, "last_error": r.last_error,
             "chunks_done": len(r.chunks or {})}
            for r in rows
        ]


# ---------- Worker ----------

_workers: List[asyncio.Task] = []
_running: set[str] = set()


async def _heartbeat_loop(job_id: str) -> None:
    while True:
        await asyncio.sleep(settings.job_heartbeat_sec)
        await asyncio.to_thread(heartbeat, job_id)


async def execute(ctx: JobCtx) -> None:
    from app.services import meeting_store, processing

    hb = asyncio.create_task(_heartbeat_loop(ctx.job_id))
    _running.add(ctx.job_id)
    from app.services import billing

    try:
        ok = await asyncio.wait_for(processing.run(ctx.meeting_id, ctx), timeout=settings.job_timeout_sec)
        # Ажлыг "дууссан" гэж тэмдэглэхээс ӨМНӨ минутыг эцэслэнэ (бичвэр хадгалагдсан бол хасна, үгүй бол чөлөөлнө)
        await asyncio.to_thread(billing.settle, ctx.meeting_id)
        complete(ctx.job_id, bool(ok))
    except RetryLater as e:
        delay = requeue(ctx.job_id, str(e), ctx.attempts)
        logger.warning("Job %s will retry in %.0fs (attempt %d/%d): %s", ctx.job_id, delay, ctx.attempts, ctx.max_attempts, e)
    except asyncio.TimeoutError:
        err = "TIMEOUT: Боловсруулалт хэт удсан тул зогсоолоо. Богино бичлэгээр дахин оролдоно уу."
        meeting_store.finish_processing(ctx.meeting_id, err)
        await asyncio.to_thread(billing.settle, ctx.meeting_id)
        complete(ctx.job_id, False, err)
    except asyncio.CancelledError:
        raise  # shutdown: ажил "running" хэвээр үлдэж, дараагийн эхлэлд lease дуусаад үргэлжилнэ
    except Exception as e:  # noqa: BLE001
        logger.exception("Job %s crashed", ctx.job_id)
        err = f"INTERNAL_ERROR: {type(e).__name__}"
        meeting_store.finish_processing(ctx.meeting_id, err)
        await asyncio.to_thread(billing.settle, ctx.meeting_id)
        complete(ctx.job_id, False, err)
    finally:
        hb.cancel()
        _running.discard(ctx.job_id)


async def _worker(idx: int) -> None:
    while True:
        try:
            ctx = await asyncio.to_thread(claim_next)
        except Exception:  # noqa: BLE001 — DB түр алдаа worker-ыг үл зогсооно
            logger.exception("Job claim failed")
            ctx = None
        if ctx is None:
            await asyncio.sleep(settings.job_poll_sec)
            continue
        await execute(ctx)


def ensure_workers() -> None:
    """Ажиллаж буй event loop дээр worker-уудыг (байхгүй бол) эхлүүлнэ."""
    loop = asyncio.get_running_loop()
    _workers[:] = [t for t in _workers if not t.done() and t.get_loop() is loop]
    while len(_workers) < max(1, settings.job_workers):
        _workers.append(loop.create_task(_worker(len(_workers)), name=f"job-worker-{len(_workers)}"))


async def shutdown() -> None:
    for t in _workers:
        t.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()


def _idle() -> bool:
    with session_scope() as s:
        return s.scalar(select(ProcessingJob.id).where(ProcessingJob.state.in_(("queued", "running"))).limit(1)) is None


async def wait_idle(timeout: float = 60.0) -> None:
    """Тест/graceful shutdown: бүх ажил дуустал хүлээнэ."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not await asyncio.to_thread(_idle):
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("jobs did not finish")
        await asyncio.sleep(0.05)
