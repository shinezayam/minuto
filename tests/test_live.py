"""Хурлын үеэр шууд бичвэр гаргах (live_transcriber)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import asr as asr_mod
from app.services import live_transcriber
from app.services.audio_utils import ffmpeg_exe
from app.services.live_transcriber import LiveTranscriber, choose_cut

from tests.test_providers import needs_ffmpeg
from tests.test_websocket import _AuthedClient, _signup


class _RecordingASR:
    """Ирсэн wav-ийн уртыг тэмдэглээд дугаартай бичвэр буцаана."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def transcribe(self, audio_path, on_progress=None):  # pragma: no cover — шууд бичвэрт хэрэглэхгүй
        return []

    async def transcribe_wav_bytes(self, wav: bytes) -> str:
        self.calls.append(len(wav))
        return f"хэсэг {len(self.calls)}"


@pytest.fixture
def fake_asr(monkeypatch):
    fake = _RecordingASR()
    monkeypatch.setattr(asr_mod, "asr_provider", fake)
    monkeypatch.setattr(settings, "live_min_sec", 2)
    monkeypatch.setattr(settings, "live_window_sec", 4)
    monkeypatch.setattr(settings, "live_max_sec", 6)
    return fake


def _make_webm(path: Path, lavfi: str) -> bytes:
    subprocess.run(
        [ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", lavfi,
         "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(path)],
        check=True,
    )
    return path.read_bytes()


def _slice_into(meeting_dir: Path, blob: bytes, parts: int) -> int:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    step = len(blob) // parts + 1
    n = 0
    for i in range(0, len(blob), step):
        (meeting_dir / f"{n:06d}.webm").write_bytes(blob[i:i + step])
        n += 1
    return n


async def _wait_for(cond, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return cond()


# ---------- таслах цэг сонгох ----------

def test_choose_cut_waits_until_target():
    assert choose_cut([1000.0] * 30, 20, 40, 60) is None


def test_choose_cut_prefers_silence():
    rms = [1000.0] * 50
    rms[33] = 10.0  # чимээгүй frame
    assert choose_cut(rms, 20, 40, 60) == 34


def test_choose_cut_waits_for_silence_then_forces_at_max():
    loud = [1000.0] * 45
    assert choose_cut(loud, 20, 40, 60) is None  # чимээгүй алга, max-д хүрээгүй
    loud = [1000.0] * 60
    assert choose_cut(loud, 20, 40, 60) is not None


# ---------- урсгалаар задлах ----------

@needs_ffmpeg
async def test_live_transcribes_streamed_chunks(tmp_path, fake_asr):
    # 1 сек дуу / 0.5 сек чимээгүй ээлжилсэн 14 сек — яриа шиг таслах цэгтэй
    blob = _make_webm(tmp_path / "rec.webm", "sine=frequency=300:duration=14,volume='if(lt(mod(t,1.5),1),1,0)':eval=frame")
    meeting_dir = tmp_path / "m"
    n = _slice_into(meeting_dir, blob, 7)

    lt = LiveTranscriber("m1", meeting_dir)
    got: list[dict] = []

    async def listener(msg):
        got.append(msg)

    lt.listeners.add(listener)
    lt.start()
    try:
        for _ in range(n):
            await lt.notify_new_audio()
        assert await _wait_for(lambda: len(lt.segments) >= 2)
        lt.request_flush()
        await _wait_for(lambda: lt._frame_rms == [] and len(fake_asr.calls) >= 3, timeout=5)
    finally:
        await lt.close()

    segs = lt.segments
    assert [s["segment_id"] for s in segs] == [f"live_{i + 1}" for i in range(len(segs))]
    assert segs[0]["start_ms"] == 0
    for a, b in zip(segs, segs[1:]):
        assert a["end_ms"] == b["start_ms"]  # цаг давхцахгүй, завсаргүй
    assert segs[-1]["end_ms"] >= 13_000
    for s in segs:
        assert settings.live_min_sec * 1000 <= s["end_ms"] - s["start_ms"] <= settings.live_max_sec * 1000 or s is segs[-1]
    assert got == segs  # сонсогч бүх хэсгийг авсан


@needs_ffmpeg
async def test_live_skips_silence(tmp_path, fake_asr):
    blob = _make_webm(tmp_path / "s.webm", "anullsrc=r=48000:cl=mono:d=10")
    meeting_dir = tmp_path / "m"
    n = _slice_into(meeting_dir, blob, 3)
    lt = LiveTranscriber("m2", meeting_dir)
    lt.start()
    try:
        for _ in range(n):
            await lt.notify_new_audio()
        lt.request_flush()
        await _wait_for(lambda: lt._consumed_samples >= 9 * 16000, timeout=10)
    finally:
        await lt.close()
    assert fake_asr.calls == []  # чимээгүйг ASR руу илгээхгүй
    assert lt.segments == []


@needs_ffmpeg
async def test_live_waits_for_missing_chunk(tmp_path, fake_asr):
    """Дараалал тасарвал (1-р хэсэг ирээгүй) дараагийнхыг нь ffmpeg руу хийхгүй."""
    blob = _make_webm(tmp_path / "rec.webm", "sine=duration=6")
    meeting_dir = tmp_path / "m"
    _slice_into(meeting_dir, blob, 3)
    missing = meeting_dir / "000001.webm"
    held = missing.read_bytes()
    missing.unlink()

    lt = LiveTranscriber("m3", meeting_dir)
    lt.start()
    try:
        await lt.notify_new_audio()
        assert lt._next_seq == 1
        missing.write_bytes(held)
        await lt.notify_new_audio()
        assert lt._next_seq == 3
    finally:
        await lt.close()


async def test_live_disabled_for_mp4(tmp_path, fake_asr):
    meeting_dir = tmp_path / "m"
    meeting_dir.mkdir()
    (meeting_dir / "000000.m4a").write_bytes(b"x")
    lt = LiveTranscriber("m4", meeting_dir)
    lt.start()
    try:
        await lt.notify_new_audio()
        assert lt.disabled_reason
    finally:
        await lt.close()


# ---------- WebSocket ----------

@needs_ffmpeg
def test_websocket_streams_live_segments(isolated_env, tmp_path, fake_asr):
    blob = _make_webm(tmp_path / "rec.webm", "sine=frequency=300:duration=9,volume='if(lt(mod(t,1.5),1),1,0)':eval=frame")
    step = len(blob) // 3 + 1
    chunks = [blob[i:i + step] for i in range(0, len(blob), step)]

    with TestClient(app) as tc:
        client = _AuthedClient(tc, _signup(tc, "live@test.mn"))
        mid = client.post("/api/meetings", json={"title": "live"}).json()["id"]
        client.post(f"/api/meetings/{mid}/start")

        with client.websocket_connect(f"/ws/meetings/{mid}/audio") as ws:
            assert ws.receive_json()["type"] == "recording.ready"
            for seq, data in enumerate(chunks):
                ws.send_text(json.dumps({"type": "audio.chunk", "sequence": seq, "mime_type": "audio/webm", "timestamp_ms": 0}))
                ws.send_bytes(data)
            seen = []
            while not any(m["type"] == "transcript.segment" for m in seen):
                seen.append(ws.receive_json())
            seg = next(m for m in seen if m["type"] == "transcript.segment")
            assert seg["text"] == "хэсэг 1" and seg["start_ms"] == 0

        # Дахин холбогдоход өмнөх бичвэр дахин ирнэ
        with client.websocket_connect(f"/ws/meetings/{mid}/audio") as ws:
            assert ws.receive_json()["type"] == "recording.ready"
            assert ws.receive_json()["segment_id"] == "live_1"

        # Дуусгахад шууд бичвэр зогсоно
        client.post(f"/api/meetings/{mid}/end")
        assert live_transcriber.get(mid) is None


def test_websocket_no_live_for_completed_meeting(isolated_env, fake_asr):
    with TestClient(app) as tc:
        client = _AuthedClient(tc, _signup(tc, "done@test.mn"))
        mid = client.post("/api/meetings", json={"title": "x"}).json()["id"]
        client.post(f"/api/meetings/{mid}/start")
        client.post(f"/api/meetings/{mid}/end")
        with client.websocket_connect(f"/ws/meetings/{mid}/audio") as ws:
            ws.receive_json()
        assert live_transcriber.get(mid) is None
