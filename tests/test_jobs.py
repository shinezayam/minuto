from __future__ import annotations

import uuid
import json
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db import Meeting, ProcessingJob, session_scope
from app.services import asr as asr_mod
from app.services import jobs, meeting_store, processing
from app.services import llm as llm_mod
from tests.test_providers import _chat, _make_tone, _patch_httpx, _sse, needs_ffmpeg, qwen_key  # noqa: F401


def _job_rows(mid: str):
    with session_scope() as s:
        return [
            {"state": j.state, "attempts": j.attempts, "chunks": dict(j.chunks or {}), "error": j.last_error}
            for j in s.scalars(select(ProcessingJob).where(ProcessingJob.meeting_id == mid).order_by(ProcessingJob.created_at))
        ]


async def _meeting_with_audio(client: AsyncClient, tmp_path, seconds=35) -> str:
    audio = tmp_path / "in.mp3"
    _make_tone(audio, seconds)
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    r = await client.post(f"/api/meetings/{mid}/upload", files={"file": ("in.mp3", audio.read_bytes(), "audio/mpeg")})
    assert r.status_code == 200, r.text
    return mid


@pytest.fixture
def omni(monkeypatch, qwen_key):
    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    monkeypatch.setattr(settings, "asr_concurrency", 1)  # дараалуулж, хэсэг бүрийг тодорхой болгоно
    monkeypatch.setattr(settings, "job_backoff_sec", 0)
    monkeypatch.setattr(asr_mod, "asr_provider", asr_mod.QwenOmniASRProvider())
    monkeypatch.setattr(llm_mod, "llm_provider", llm_mod.QwenLLMProvider())


def _handler(audio_calls: list, fail_on_call: set[int] | None = None, fail_always: bool = False):
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if body.get("stream"):  # ASR хэсэг
            audio_calls.append(1)
            n = len(audio_calls)
            if fail_always or (fail_on_call and n in fail_on_call):
                return httpx.Response(401, json={"error": {"message": "Invalid API-key"}})  # дахин оролдохгүй алдаа → ProviderError
            return httpx.Response(200, content=_sse(f"хэсэг {n}"))
        return _chat({"title": "Т", "summary": "С"})

    return handler


@needs_ffmpeg
async def test_resume_skips_chunks_already_done(client, tmp_path, omni, monkeypatch):
    """4 хэсэгтэй бичлэг: 3-р хэсэг дээр provider алдлаа → ажил дахин оролдож, дууссан 2 хэсгийг дахин дуудахгүй."""
    mid = await _meeting_with_audio(client, tmp_path, seconds=35)  # 10 сек × 4 хэсэг
    calls: list = []
    _patch_httpx(monkeypatch, _handler(calls, fail_on_call={3}))

    assert (await client.post(f"/api/meetings/{mid}/process")).status_code == 202
    await processing.wait_all()

    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["status"] == "completed", m
    # 1,2 амжилттай; 3 унасан; дахин оролдлого: 3,4 → нийт 5 дуудлага (8 биш)
    assert len(calls) == 5
    jobs_ = _job_rows(mid)
    assert len(jobs_) == 1 and jobs_[0]["state"] == "succeeded" and jobs_[0]["attempts"] == 2
    assert len(jobs_[0]["chunks"]) == 4
    segs = (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]
    assert len(segs) == 4


@needs_ffmpeg
async def test_poison_job_stops_at_max_attempts_with_clear_error(client, tmp_path, omni, monkeypatch):
    monkeypatch.setattr(settings, "job_max_attempts", 3)
    mid = await _meeting_with_audio(client, tmp_path, seconds=8)  # 1 хэсэг → оролдлого бүрд 1 дуудлага
    calls: list = []
    _patch_httpx(monkeypatch, _handler(calls, fail_always=True))

    await client.post(f"/api/meetings/{mid}/process")
    await processing.wait_all()

    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["status"] == "failed" and m["processing_error"].startswith("ASR_FAILED")
    j = _job_rows(mid)[0]
    assert j["state"] == "failed" and j["attempts"] == 3  # max_attempts-д зогссон
    assert len(calls) == 3


@needs_ffmpeg
async def test_non_provider_errors_are_not_retried(client, tmp_path, omni, monkeypatch):
    mid = await _meeting_with_audio(client, tmp_path, seconds=15)
    monkeypatch.setattr(asr_mod, "asr_provider", type("Empty", (), {"name": "x", "transcribe": staticmethod(lambda *a, **k: _empty())})())

    async def _empty():
        return []

    await client.post(f"/api/meetings/{mid}/process")
    await processing.wait_all()
    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["processing_error"].startswith("NO_SPEECH")
    assert _job_rows(mid)[0]["attempts"] == 1  # түр алдаа биш → дахин оролдоогүй


async def test_double_process_creates_exactly_one_job(client: AsyncClient, tmp_path, monkeypatch):
    from tests.test_upload_protection import wav_bytes

    real_ensure = jobs.ensure_workers
    monkeypatch.setattr(jobs, "ensure_workers", lambda: None)  # ажил дараалалд үлдэнэ (шууд дуусахгүй)

    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    await client.post(f"/api/meetings/{mid}/upload", files={"file": ("a.wav", wav_bytes(), "audio/wav")})
    codes = [(await client.post(f"/api/meetings/{mid}/process")).status_code for _ in range(3)]
    assert codes == [202, 409, 409]
    assert len(_job_rows(mid)) == 1 and _job_rows(mid)[0]["state"] == "queued"
    monkeypatch.setattr(jobs, "ensure_workers", real_ensure)  # цэвэрлэгээ: ажлыг дуусгана
    real_ensure()
    await processing.wait_all()


def test_database_blocks_a_second_active_job(client=None):
    # Хурал бэлдээд, DB-ийн unique guard-ыг шууд шалгана (app-ын шалгалтаас гадна)
    from app.services.auth import register

    user = register("guard@test.mn", "password123")
    meeting_id = meeting_store.create_meeting(user.id, __import__("app.models.schemas", fromlist=["MeetingCreate"]).MeetingCreate(title="G")).id
    jobs.enqueue(meeting_id)
    with pytest.raises(jobs.AlreadyActive):
        jobs.enqueue(meeting_id)


# ---------- Унасан ажлыг сэргээх ----------

def _make_meeting():
    from app.models.schemas import MeetingCreate
    from app.services.auth import register

    user = register(f"u{uuid.uuid4().hex[:12]}@test.mn", "password123")  # id(object()) давтагдаж болно
    return meeting_store.create_meeting(user.id, MeetingCreate(title="J")).id


def _set(job_id, **values):
    with session_scope() as s:
        for k, v in values.items():
            setattr(s.get(ProcessingJob, job_id), k, v)


def test_expired_lease_job_is_reclaimed_with_saved_chunks():
    mid = _make_meeting()
    meeting_store.try_begin_processing(mid)
    jid = jobs.enqueue(mid)
    ctx = jobs.claim_next()
    assert ctx.job_id == jid and ctx.attempts == 1
    ctx.save_chunk(0, {"text": "а", "start_ms": 0, "end_ms": 10})
    assert jobs.claim_next() is None  # lease хүчинтэй: өөр worker авахгүй

    _set(jid, lease_expires_at=jobs._now() - timedelta(seconds=1))  # worker унасан
    again = jobs.claim_next()
    assert again.job_id == jid and again.attempts == 2 and again.chunks == {0: {"text": "а", "start_ms": 0, "end_ms": 10}}


def test_startup_requeues_running_jobs_and_fails_orphans():
    running, orphan = _make_meeting(), _make_meeting()
    meeting_store.try_begin_processing(running)
    jid = jobs.enqueue(running)
    jobs.claim_next()
    meeting_store.try_begin_processing(orphan)  # job-гүй "processing" (хуучин процессоос үлдсэн)

    out = jobs.recover_on_startup()
    assert out == {"requeued": 1, "orphans": 1}
    with session_scope() as s:
        assert s.get(Meeting, orphan).status == "failed" and s.get(Meeting, orphan).processing_error.startswith("INTERRUPTED")
        assert s.get(Meeting, running).status == "processing"  # ажил нь үргэлжилнэ
    assert jobs.claim_next().job_id == jid  # шууд дахин авна


def test_crashing_over_and_over_is_stopped():
    mid = _make_meeting()
    meeting_store.try_begin_processing(mid)
    jid = jobs.enqueue(mid)
    for _ in range(settings.job_max_attempts):
        assert jobs.claim_next().job_id == jid
        _set(jid, lease_expires_at=jobs._now() - timedelta(seconds=1))
    assert jobs.claim_next() is None  # дахин авахгүй, эцсийн алдаатай болгоно
    j = jobs.list_jobs(mid)[0]
    assert j["state"] == "failed" and j["last_error"].startswith("INTERRUPTED")
    with session_scope() as s:
        assert s.get(Meeting, mid).status == "failed"


def test_heartbeat_extends_lease():
    mid = _make_meeting()
    jid = jobs.enqueue(mid)
    jobs.claim_next()
    _set(jid, lease_expires_at=jobs._now() + timedelta(seconds=1))
    jobs.heartbeat(jid)
    with session_scope() as s:
        lease = s.get(ProcessingJob, jid).lease_expires_at
    assert lease.replace(tzinfo=None) > (jobs._now() + timedelta(seconds=settings.job_lease_sec - 5)).replace(tzinfo=None)


def test_requeue_backoff_delays_the_next_claim(monkeypatch):
    monkeypatch.setattr(settings, "job_backoff_sec", 60)
    mid = _make_meeting()
    jid = jobs.enqueue(mid)
    ctx = jobs.claim_next()
    assert jobs.requeue(jid, "ASR_FAILED", ctx.attempts) == 60
    assert jobs.claim_next() is None  # backoff дуустал авахгүй


async def test_cannot_process_while_recording(client: AsyncClient):
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    await client.post(f"/api/meetings/{mid}/start")
    assert meeting_store.try_begin_processing(mid) is False
