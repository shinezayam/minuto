"""AI зардлын бүртгэл (ai_usage): Qwen токен, Scribe секунд, хурал/хэрэглэгчийн хамаарал, админы тайлан."""
from __future__ import annotations

import io
import struct
import wave

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import AiUsage, Meeting, User, session_scope
from app.services import ai_usage
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from tests.conftest import register


def _patch_httpx(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _rows() -> list[AiUsage]:
    with session_scope() as s:
        return list(s.scalars(select(AiUsage).order_by(AiUsage.created_at)).all())


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
def prices(monkeypatch):
    monkeypatch.setattr(settings, "price_scribe_usd_per_hour", 0.20)
    monkeypatch.setattr(settings, "price_scribe_keyterms_usd_per_hour", 0.05)
    monkeypatch.setattr(settings, "price_llm_input_usd_per_m", 2.0)
    monkeypatch.setattr(settings, "price_llm_output_usd_per_m", 6.0)
    monkeypatch.setattr(settings, "usd_to_mnt", 3600.0)


async def test_qwen_call_records_tokens_and_cost_for_the_scoped_meeting(monkeypatch, prices):
    monkeypatch.setattr(settings, "qwen_api_key", "k")

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 30_000, "completion_tokens": 4_000}})

    _patch_httpx(monkeypatch, handler)
    provider = llm_mod.QwenLLMProvider()
    with ai_usage.scope(meeting_id="m-1", llm_purpose="llm_summary"):
        await provider.complete_json([{"role": "user", "content": "x"}])
    [r] = _rows()
    assert (r.meeting_id, r.purpose, r.provider, r.input_tokens, r.output_tokens) == ("m-1", "llm_summary", "qwen", 30_000, 4_000)
    assert r.cost_usd == pytest.approx(0.06 + 0.024)


async def test_scribe_live_window_records_audio_seconds(monkeypatch, prices):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el")
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json={"text": "Сайн"}))
    provider = asr_mod.ScribeASRProvider()
    with ai_usage.scope(meeting_id="m-2"):
        await provider.transcribe_wav_bytes(_wav(18), context=asr_mod.ASRContext(hints=["Минуто"]))
    [r] = _rows()
    assert (r.meeting_id, r.purpose, r.audio_seconds) == ("m-2", "asr_live", pytest.approx(18.0))
    assert r.cost_usd == pytest.approx(18 / 3600 * 0.25)  # keyterms-тэй


async def test_scribe_file_records_duration_from_words(tmp_path, monkeypatch, prices):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el")
    words = [{"text": "Сайн", "start": 0.0, "end": 1.0, "type": "word", "speaker_id": "speaker_0"},
             {"text": "уу", "start": 3599.0, "end": 3600.0, "type": "word", "speaker_id": "speaker_0"}]
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json={"text": "Сайн уу", "words": words}))
    audio = tmp_path / "uploaded.mp3"
    audio.write_bytes(b"x")
    await asr_mod.ScribeASRProvider().transcribe(str(audio))
    [r] = _rows()
    assert (r.purpose, r.audio_seconds, r.cost_usd) == ("asr_file", 3600.0, pytest.approx(0.20))


async def test_recording_failure_never_breaks_the_call(monkeypatch, prices):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el")
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json={"text": "ok"}))

    def boom(row):
        raise RuntimeError("db down")

    monkeypatch.setattr(ai_usage, "session_scope", boom)
    assert await asr_mod.ScribeASRProvider().transcribe_wav_bytes(_wav(1)) == "ok"


async def test_admin_report_totals_and_permissions(anon_client, monkeypatch, prices):
    monkeypatch.setattr(settings, "admin_emails", "boss@minuto.mn")
    boss = await register(anon_client, "boss@minuto.mn")
    user = await register(anon_client, "user@minuto.mn")
    with session_scope() as s:
        uid = s.scalar(select(User.id).where(User.email == "user@minuto.mn"))
        m = Meeting(owner_id=uid, title="Т")
        s.add(m)
        s.flush()
        mid = m.id
        s.add_all([
            AiUsage(meeting_id=mid, purpose="asr_file", provider="scribe", audio_seconds=3600, cost_usd=0.20),
            AiUsage(meeting_id=mid, purpose="llm_minutes", provider="qwen", input_tokens=30_000, output_tokens=4_000,
                    cost_usd=0.084),
            AiUsage(user_id=uid, purpose="llm_ask", provider="qwen", input_tokens=20_000, output_tokens=500,
                    cost_usd=0.043),
        ])
    r = await anon_client.get("/api/admin/ai-usage", headers={"Authorization": f"Bearer {user['token']}"})
    assert r.status_code == 403
    r = await anon_client.get("/api/admin/ai-usage?days=7", headers={"Authorization": f"Bearer {boss['token']}"})
    assert r.status_code == 200
    rep = r.json()
    assert rep["totals"]["requests"] == 3 and rep["totals"]["cost_usd"] == pytest.approx(0.327)
    assert rep["totals"]["cost_mnt"] == round(0.327 * 3600)
    assert [p["purpose"] for p in rep["by_purpose"]] == ["asr_file", "llm_minutes", "llm_ask"]
    assert len(rep["by_day"]) == 7 and rep["by_day"][-1]["cost_usd"] == pytest.approx(0.327)
    [top] = rep["top_users"]
    assert top["email"] == "user@minuto.mn" and top["meetings"] == 1 and top["cost_usd"] == pytest.approx(0.327)
