from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from app.models.minutes import MeetingMinutes, TranscriptSegment
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.services import meeting_store, processing, vocabulary
from app.services.asr import ASRContext, build_omni_prompt


async def _me(client: AsyncClient) -> str:
    return (await client.get("/api/auth/me")).json()["id"]


def test_prompt_without_context_is_unchanged():
    assert build_omni_prompt(ASRContext()) == asr_mod.OMNI_PROMPT


def test_prompt_with_context():
    p = build_omni_prompt(ASRContext(hints=["Баатар", "ХААН банк"], topic="Төсөв"))
    assert p.startswith(asr_mod.OMNI_PROMPT)
    assert "Хурлын сэдэв: Төсөв." in p and "Баатар, ХААН банк" in p and "бүү нэм" in p


@pytest.mark.asyncio
async def test_vocabulary_crud(client: AsyncClient):
    r = await client.post("/api/vocabulary", json={"term": "ХААН банк"})
    assert r.status_code == 201
    tid = r.json()["id"]
    assert (await client.post("/api/vocabulary", json={"term": "ХААН банк"})).json()["id"] == tid  # давхардахгүй
    assert (await client.post("/api/vocabulary", json={"term": "  "})).status_code == 422
    terms = (await client.get("/api/vocabulary")).json()["terms"]
    assert [t["term"] for t in terms] == ["ХААН банк"]
    assert (await client.delete(f"/api/vocabulary/{tid}")).status_code == 204
    assert (await client.delete(f"/api/vocabulary/{tid}")).status_code == 404
    assert (await client.get("/api/vocabulary")).json()["terms"] == []


@pytest.mark.asyncio
async def test_remember_correction_rules(client: AsyncClient):
    uid = await _me(client)
    vocabulary.remember_correction(uid, "Бат", "Баатар")
    vocabulary.remember_correction(uid, "бат", "Батаа")  # ижил буруу үг → шинэчилнэ
    assert [(t.wrong, t.term) for t in vocabulary.list_terms(uid)] == [("Бат", "Батаа")]
    vocabulary.remember_correction(uid, "Батаа", "Батбаяр")  # зөв үгийг дахин засвал гинжлэнэ
    pairs = {(t.wrong, t.term) for t in vocabulary.list_terms(uid)}
    assert pairs == {("Бат", "Батбаяр"), ("Батаа", "Батбаяр")}
    vocabulary.remember_correction(uid, "Батбаяр", "Бат")  # эсрэг засвар цуцална
    assert {(t.wrong, t.term) for t in vocabulary.list_terms(uid)} == {("Батаа", "Бат")}
    vocabulary.remember_correction(uid, "x", "")  # хоосон бол хадгалахгүй
    assert len(vocabulary.list_terms(uid)) == 1


@pytest.mark.asyncio
async def test_replace_remembers_unless_disabled(client: AsyncClient):
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    meeting_store.update_meeting_transcript(mid, [{"id": "s1", "start_ms": 0, "end_ms": 1, "text": "Бат ирэв. Дорж ирэв."}])
    r = await client.post(f"/api/meetings/{mid}/transcript/replace", json={"find": "Бат", "replace": "Баатар"})
    assert r.json()["remembered"] is True
    r = await client.post(
        f"/api/meetings/{mid}/transcript/replace", json={"find": "Дорж", "replace": "Доржоо", "remember": False}
    )
    assert r.json()["remembered"] is False
    r = await client.post(f"/api/meetings/{mid}/transcript/replace", json={"find": "байхгүй", "replace": "x"})
    assert r.json()["remembered"] is False  # юу ч олдоогүй
    terms = (await client.get("/api/vocabulary")).json()["terms"]
    assert [(t["wrong"], t["term"]) for t in terms] == [("Бат", "Баатар")]


@pytest.mark.asyncio
async def test_for_meeting_hints(client: AsyncClient):
    uid = await _me(client)
    mid = (await client.post("/api/meetings", json={"title": "Шинэ хурал", "participants": ["Дорж", "Баатар"]})).json()["id"]
    vocabulary.add_term(uid, "ХААН банк")
    vocabulary.add_term(uid, "баатар")  # оролцогчтой давхардана
    vocabulary.remember_correction(uid, "Бат", "Баатар")
    vocabulary.remember_correction(uid, "Бат Эрдэнэ", "Батэрдэнэ")
    v = vocabulary.for_meeting(mid)
    assert v.topic is None  # анхны гарчиг сэдэв биш
    assert v.hints[:2] == ["Дорж", "Баатар"] and "ХААН банк" in v.hints and "Батэрдэнэ" in v.hints
    assert len([h for h in v.hints if h.casefold() == "баатар"]) == 1
    assert v.corrections[0] == ("Бат Эрдэнэ", "Батэрдэнэ")  # урт нь эхэндээ
    segs, n = vocabulary.apply_to_segments(
        [{"id": "s", "start_ms": 0, "end_ms": 1, "text": "Бат Эрдэнэ, бат хоёр", "turns": []}], v.corrections
    )
    assert segs[0]["text"] == "Батэрдэнэ, Баатар хоёр" and n == 2


@pytest.mark.asyncio
async def test_processing_uses_vocabulary(client: AsyncClient, monkeypatch, tmp_path):
    uid = await _me(client)
    mid = (await client.post("/api/meetings", json={"title": "Төсвийн хурал", "participants": ["Дорж"]})).json()["id"]
    vocabulary.remember_correction(uid, "Бат", "Баатар")
    seen: dict = {}

    class FakeASR:
        name = "fake"

        async def transcribe(self, audio_path, on_progress=None, context=asr_mod.NO_CONTEXT):
            seen["context"] = context
            return [TranscriptSegment(id="s1", start_ms=0, end_ms=1000, text="Бат тайлан өгнө.")]

    class FakeLLM:
        async def generate_minutes(self, transcript, participants):
            seen["llm_text"] = transcript[0].text
            return MeetingMinutes(title="Т", summary="С")

    audio = tmp_path / "uploaded.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(processing, "find_meeting_audio", lambda d: Path(audio))
    monkeypatch.setattr(asr_mod, "asr_provider", FakeASR())
    monkeypatch.setattr(llm_mod, "llm_provider", FakeLLM())
    assert meeting_store.try_begin_processing(mid)
    await processing.run(mid)

    assert seen["context"].topic == "Төсвийн хурал"
    assert seen["context"].hints == ["Дорж", "Баатар"]
    assert seen["llm_text"] == "Баатар тайлан өгнө."  # LLM засагдсан бичвэрийг авна
    segs = (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]
    assert segs[0]["text"] == "Баатар тайлан өгнө."
