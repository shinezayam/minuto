"""Хөтлөгчийн гар тэмдэглэл: HTML цэвэрлэгээ, хадгалалт, AI prompt-д орох, экспорт."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from app.models.minutes import MeetingMinutes, TranscriptSegment
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.services import meeting_store, processing
from app.services.notes import notes_to_text, sanitize_notes_html


def test_sanitizer_keeps_formatting_and_strips_everything_else():
    raw = (
        '<div style="x">Бат: <strong>төсөв</strong> <script>alert(1)</script><img src=x onerror=alert(1)>'
        '<a href="javascript:alert(1)" onclick="x">линк</a></div>'
        "<ul><li>нэг</li><li>хоёр<ol><li>а</li></ol></li></ul><u>доогуур</u> <em>налуу</em>"
    )
    out = sanitize_notes_html(raw)
    assert "<script" not in out and "alert" not in out and "onerror" not in out and "href" not in out and "style" not in out
    assert "<b>төсөв</b>" in out and "<u>доогуур</u>" in out and "<i>налуу</i>" in out and "<ol><li>а</li></ol>" in out
    assert notes_to_text(out).splitlines() == ["Бат: төсөв линк", "- нэг", "- хоёр", "  1. а", "доогуур налуу"]
    assert sanitize_notes_html("<p><br></p>  ") is None
    assert sanitize_notes_html("a < b & c") == "a &lt; b &amp; c"


async def test_notes_saved_and_returned(client: AsyncClient):
    mid = (await client.post("/api/meetings", json={"title": "Хурал"})).json()["id"]
    r = await client.patch(f"/api/meetings/{mid}", json={"notes": "<p>Шийдвэр: <b>Баасан</b></p><script>x</script>"})
    assert r.status_code == 200 and r.json()["notes"] == "<p>Шийдвэр: <b>Баасан</b></p>"
    assert (await client.get(f"/api/meetings/{mid}")).json()["notes"] == "<p>Шийдвэр: <b>Баасан</b></p>"
    # гарчиг солиход тэмдэглэл хэвээр; "" бол арилна
    await client.patch(f"/api/meetings/{mid}", json={"title": "Шинэ"})
    assert (await client.get(f"/api/meetings/{mid}")).json()["notes"]
    await client.patch(f"/api/meetings/{mid}", json={"notes": ""})
    assert (await client.get(f"/api/meetings/{mid}")).json()["notes"] is None


@pytest.mark.asyncio
async def test_processing_passes_notes_to_llm(client: AsyncClient, monkeypatch, tmp_path):
    mid = (await client.post("/api/meetings", json={"title": "Хурал"})).json()["id"]
    await client.patch(f"/api/meetings/{mid}", json={"notes": "<ul><li>Төсөв 5 сая</li></ul>"})
    seen: dict = {}

    class FakeASR:
        name = "fake"

        async def transcribe(self, audio_path, on_progress=None, context=asr_mod.NO_CONTEXT):
            return [TranscriptSegment(id="s1", start_ms=0, end_ms=1000, text="Төсөв ... сая")]

    class FakeLLM:
        async def generate_minutes(self, transcript, participants=None, template=None, language="mn", notes=None):
            seen["notes"] = notes
            return MeetingMinutes(title="Т", summary="С")

    audio = tmp_path / "uploaded.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(processing, "find_meeting_audio", lambda d: Path(audio))
    monkeypatch.setattr(asr_mod, "asr_provider", FakeASR())
    monkeypatch.setattr(llm_mod, "llm_provider", FakeLLM())
    assert meeting_store.try_begin_processing(mid)
    await processing.run(mid)
    assert seen["notes"] == "- Төсөв 5 сая"


async def test_qwen_prompt_includes_notes(monkeypatch):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"title": "Х", "summary": "Т"})}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(llm_mod.settings, "qwen_api_key", "sk-test")
    monkeypatch.setattr(
        llm_mod.httpx,
        "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler), **{x: y for x, y in k.items() if x != "transport"}),
    )
    seg = [TranscriptSegment(id="s1", start_ms=0, end_ms=1000, text="сонсогдохгүй")]
    await llm_mod.QwenLLMProvider().generate_minutes(seg, None, notes="- Төсөв 5 сая")
    user = seen["body"]["messages"][1]["content"]
    assert "Хөтлөгчийн гар тэмдэглэл" in user and "Төсөв 5 сая" in user
    # тэмдэглэлгүй бол prompt-д огт орохгүй
    await llm_mod.QwenLLMProvider().generate_minutes(seg, None)
    assert "Хөтлөгчийн гар тэмдэглэл" not in seen["body"]["messages"][1]["content"]


def test_exports_accept_notes():
    from app.services.export_docx import generate_meeting_docx
    from app.services.export_pdf import generate_meeting_pdf

    m = MeetingMinutes(title="Т", summary="С")
    assert generate_meeting_pdf("x", "Т", "2026-11-17T03:15:00", m, None, [], notes="- нэг\n  1. а")[:5] == b"%PDF-"
    assert generate_meeting_docx("x", "Т", "2026-11-17T03:15:00", m, None, [], notes="- нэг")[:2] == b"PK"
