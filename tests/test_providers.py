"""ASR / LLM provider-ууд — гадны API-г httpx.MockTransport-оор орлуулж шалгана."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.models.minutes import TranscriptSegment
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.services.audio_utils import ffmpeg_exe, find_meeting_audio, split_to_wav


def _ffmpeg_available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except RuntimeError:
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg алга")


def _make_tone(path: Path, seconds: int) -> None:
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", str(path)],
        check=True,
    )


@pytest.fixture
def qwen_key(monkeypatch):
    monkeypatch.setattr(settings, "qwen_api_key", "test-key")


def _patch_httpx(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _sse(*texts: str) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


# ---------- audio ----------

@needs_ffmpeg
async def test_split_to_wav(tmp_path):
    src = tmp_path / "a.mp3"
    _make_tone(src, 25)
    parts = await split_to_wav(src, tmp_path / "parts", chunk_sec=10)
    assert len(parts) == 3


def test_find_meeting_audio_prefers_upload(tmp_path):
    (tmp_path / "000000.webm").write_bytes(b"a")
    (tmp_path / "uploaded.mp3").write_bytes(b"x")
    assert find_meeting_audio(tmp_path).name == "uploaded.mp3"


def test_find_meeting_audio_concatenates_chunks_in_order(tmp_path):
    (tmp_path / "000001.bin").write_bytes(b"B")
    (tmp_path / "000000.webm").write_bytes(b"A")
    (tmp_path / "000002.bin").write_bytes(b"C")
    out = find_meeting_audio(tmp_path)
    assert out.name == "recorded.webm"
    assert out.read_bytes() == b"ABC"


def test_find_meeting_audio_none(tmp_path):
    assert find_meeting_audio(tmp_path / "missing") is None
    assert find_meeting_audio(tmp_path) is None


# ---------- Qwen-Omni ASR ----------

@needs_ffmpeg
async def test_omni_transcribes_each_chunk(tmp_path, qwen_key, monkeypatch):
    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    src = tmp_path / "meeting.mp3"
    _make_tone(src, 25)

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["authorization"] == "Bearer test-key"
        assert body["stream"] is True
        audio = body["messages"][0]["content"][0]["input_audio"]
        assert audio["format"] == "wav" and audio["data"].startswith("data:;base64,")
        n = len(calls)
        return httpx.Response(200, content=_sse("Сайн ", f"байна уу {n}"))

    _patch_httpx(monkeypatch, handler)
    segs = await asr_mod.QwenOmniASRProvider().transcribe(str(src))

    assert len(calls) == 3
    assert [s.id for s in segs] == ["seg_1", "seg_2", "seg_3"]
    assert segs[0].start_ms == 0 and segs[1].start_ms == 10_000
    assert 24_000 <= segs[2].end_ms <= 26_000
    assert all(s.text.startswith("Сайн байна уу") for s in segs)


@needs_ffmpeg
async def test_omni_auth_error_not_retried(tmp_path, qwen_key, monkeypatch):
    src = tmp_path / "m.mp3"
    _make_tone(src, 3)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "Invalid API-key"}})

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(asr_mod.ProviderError, match="401"):
        await asr_mod.QwenOmniASRProvider().transcribe(str(src))
    assert len(calls) == 1


def test_omni_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "qwen_api_key", "")
    with pytest.raises(RuntimeError):
        asr_mod.QwenOmniASRProvider()


# ---------- Qwen LLM ----------

TRANSCRIPT = [
    TranscriptSegment(id="seg_1", start_ms=0, end_ms=30_000, text="Төслийг ирэх сарын 1-нд хүлээлгэн өгөхөөр шийдлээ."),
    TranscriptSegment(id="seg_2", start_ms=30_000, end_ms=60_000, text="Бат тайланг баасан гаригт бэлдэнэ."),
]


def _chat(content: dict | str) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_qwen_minutes_valid(qwen_key, monkeypatch):
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["body"] = body
        return _chat({
            "title": "Төслийн хурал",
            "summary": "Хүлээлгэн өгөх хугацааг тогтов.",
            "topics": [],
            "decisions": [{"text": "1-нд хүлээлгэн өгнө", "source_segment_ids": ["seg_1", "seg_99"]}],
            "action_items": [{"task": "Тайлан бэлдэх", "owner": "Бат", "due_date": "Баасан", "source_segment_ids": ["seg_2"]}],
            "open_questions": [],
        })

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)

    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "[seg_2 00:30-01:00]" in seen["body"]["messages"][1]["content"]
    assert m.title == "Төслийн хурал"
    assert m.decisions[0].source_segment_ids == ["seg_1"]  # байхгүй seg_99 хасагдсан
    assert m.action_items[0].owner == "Бат"


async def test_qwen_minutes_repairs_invalid_json(qwen_key, monkeypatch):
    replies = iter(["not json", json.dumps({"title": "Х", "summary": "Т"})])
    _patch_httpx(monkeypatch, lambda req: _chat(next(replies)))
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)
    assert m.title == "Х"


async def test_qwen_minutes_gives_up(qwen_key, monkeypatch):
    _patch_httpx(monkeypatch, lambda req: _chat("still not json"))
    with pytest.raises(asr_mod.ProviderError):
        await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)


async def test_qwen_minutes_empty_transcript(qwen_key):
    with pytest.raises(ValueError):
        await llm_mod.QwenLLMProvider().generate_minutes([])


@needs_ffmpeg
async def test_process_endpoint_end_to_end(tmp_path, qwen_key, monkeypatch):
    """upload → process (Omni + Qwen, mock HTTP) → transcript → minutes → DOCX"""
    from httpx import ASGITransport, AsyncClient
    from app.api import meetings as meetings_api
    from app.main import app

    monkeypatch.setattr(settings, "audio_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    monkeypatch.setattr(meetings_api, "asr_provider", asr_mod.QwenOmniASRProvider())
    monkeypatch.setattr(meetings_api, "llm_provider", llm_mod.QwenLLMProvider())

    audio = tmp_path / "in.mp3"
    _make_tone(audio, 15)

    def handler(req):
        body = json.loads(req.content)
        if body.get("stream"):
            return httpx.Response(200, content=_sse("Бат тайлан бэлдэнэ."))
        return _chat({"title": "Тест", "summary": "Товч", "open_questions": [{"text": "Төсөв?"}]})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mid = (await client.post("/api/meetings", json={"title": "E2E"})).json()["id"]
        r = await client.post(f"/api/meetings/{mid}/upload",
                              files={"file": ("in.mp3", audio.read_bytes(), "audio/mpeg")})
        assert r.status_code == 200

        _patch_httpx(monkeypatch, handler)  # app-ийн client-ийг үүсгэсний дараа patch хийнэ
        r = await client.post(f"/api/meetings/{mid}/process")
        assert r.status_code == 200, r.text
        assert r.json()["transcript_segments"] == 2

        segs = (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]
        assert segs[0]["text"] == "Бат тайлан бэлдэнэ."
        minutes = (await client.get(f"/api/meetings/{mid}/minutes")).json()
        assert minutes["open_questions"][0]["text"] == "Төсөв?"

        docx = await client.get(f"/api/meetings/{mid}/export.docx")
        assert docx.status_code == 200 and docx.content[:2] == b"PK"


async def test_process_without_audio_returns_400(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    monkeypatch.setattr(settings, "audio_storage_path", str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mid = (await client.post("/api/meetings", json={"title": "x"})).json()["id"]
        r = await client.post(f"/api/meetings/{mid}/process")
        assert r.status_code == 400


async def test_qwen_http_error(qwen_key, monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(asr_mod.ProviderError, match="500"):
        await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)
