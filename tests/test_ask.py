"""Ask Minuto — хурлаас асуулт асуух."""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from app.services import ask as ask_mod
from app.services import llm as llm_mod
from app.services import meeting_store

from tests.conftest import register

TRANSCRIPT = [
    {"id": "seg_1", "start_ms": 0, "end_ms": 5000, "text": "Өнөөдөр шинэ бүтээгдэхүүний нээлтийг ярилцъя."},
    {"id": "seg_2", "start_ms": 5000, "end_ms": 12000, "text": "Маркетингийн баг сошиал сурталчилгааг ирэх долоо хоногт эхлүүлнэ."},
    {"id": "seg_3", "start_ms": 12000, "end_ms": 20000, "text": "Санхүүгийн тайланг Болд баасан гаригт өгнө."},
]


class _FakeLLM:
    """complete_json-д ирсэн мессежийг хадгалаад урьдчилан тохируулсан хариуг буцаана."""

    name = "fake"

    def __init__(self, reply: dict | str):
        self.reply = reply
        self.calls: list[list] = []

    async def complete_json(self, messages: list) -> str:
        self.calls.append(messages)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)


async def _meeting_with_transcript(client: AsyncClient, title: str, transcript=TRANSCRIPT, minutes=None) -> str:
    mid = (await client.post("/api/meetings", json={"title": title})).json()["id"]
    meeting_store.update_meeting_transcript(mid, transcript)
    if minutes:
        meeting_store.update_meeting_minutes(mid, minutes)
    return mid


def _ref_of(messages: list, text: str) -> str:
    """LLM-д өгсөн контекстоос тухайн өгүүлбэрийн шошгыг олно."""
    for line in messages[-1]["content"].splitlines():
        if text in line:
            return line.split()[0].lstrip("[")
    raise AssertionError(f"{text!r} контекстод алга")


@pytest.mark.asyncio
async def test_answer_links_to_transcript_timestamps(client: AsyncClient, monkeypatch):
    mid = await _meeting_with_transcript(client, "Долоо хоногийн хурал", minutes={"title": "x", "summary": "Нээлтийн бэлтгэл."})

    class _Echo(_FakeLLM):
        async def complete_json(self, messages):
            self.calls.append(messages)
            ref = _ref_of(messages, "Маркетингийн баг")
            return json.dumps({"answer": f"Сошиал сурталчилгааг эхлүүлнэ [{ref}].", "sources": [ref]}, ensure_ascii=False)

    fake = _Echo({})
    monkeypatch.setattr(llm_mod, "llm_provider", fake)

    r = await client.post("/api/ask", json={"question": "Маркетингийн баг ямар ажлууд хийхээр болсон бэ?"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["meetings_searched"] == 1
    [c] = data["citations"]
    assert c["meeting_id"] == mid and c["segment_id"] == "seg_2"
    assert (c["start_ms"], c["end_ms"]) == (5000, 12000)
    assert c["meeting_title"] == "Долоо хоногийн хурал"
    assert f"[{c['ref']}]" in data["answer"]

    ctx = fake.calls[0][-1]["content"]
    assert "Долоо хоногийн хурал" in ctx and "Нээлтийн бэлтгэл" in ctx
    assert fake.calls[0][0]["role"] == "system"


@pytest.mark.asyncio
async def test_invented_refs_are_dropped(client: AsyncClient, monkeypatch):
    await _meeting_with_transcript(client, "Хурал")
    monkeypatch.setattr(llm_mod, "llm_provider", _FakeLLM({"answer": "Тийм [S999].", "sources": ["S999", "bogus"]}))
    data = (await client.post("/api/ask", json={"question": "юу?"})).json()
    assert data["citations"] == []
    assert "[S999]" not in data["answer"]


@pytest.mark.asyncio
async def test_non_json_reply_is_returned_as_text(client: AsyncClient, monkeypatch):
    await _meeting_with_transcript(client, "Хурал")
    monkeypatch.setattr(llm_mod, "llm_provider", _FakeLLM("энгийн текст"))
    data = (await client.post("/api/ask", json={"question": "юу?"})).json()
    assert data["answer"] == "энгийн текст"


@pytest.mark.asyncio
async def test_scoped_to_one_meeting_and_owner(client: AsyncClient, anon_client, monkeypatch):
    a = await _meeting_with_transcript(client, "А хурал")
    await _meeting_with_transcript(client, "Б хурал", [{"id": "seg_1", "start_ms": 0, "end_ms": 1000, "text": "Б-гийн яриа"}])
    fake = _FakeLLM({"answer": "ok", "sources": []})
    monkeypatch.setattr(llm_mod, "llm_provider", fake)

    r = await client.post("/api/ask", json={"question": "юу?", "meeting_id": a})
    assert r.json()["meetings_searched"] == 1
    assert "Б-гийн яриа" not in fake.calls[-1][-1]["content"]

    r = await client.post("/api/ask", json={"question": "юу?"})
    assert r.json()["meetings_searched"] == 2

    # Өөр хэрэглэгч бусдын хурлаас асууж чадахгүй
    other = await register(anon_client)
    headers = {"Authorization": f"Bearer {other['token']}"}
    assert (await anon_client.post("/api/ask", json={"question": "юу?", "meeting_id": a}, headers=headers)).status_code == 404
    assert (await anon_client.post("/api/ask", json={"question": "юу?"}, headers=headers)).json()["meetings_searched"] == 0


@pytest.mark.asyncio
async def test_requires_login(anon_client: AsyncClient):
    assert (await anon_client.post("/api/ask", json={"question": "юу?"})).status_code == 401


@pytest.mark.asyncio
async def test_history_is_passed_and_used_for_search(client: AsyncClient, monkeypatch):
    await _meeting_with_transcript(client, "Хурал")
    fake = _FakeLLM({"answer": "ok", "sources": []})
    monkeypatch.setattr(llm_mod, "llm_provider", fake)
    history = [
        {"role": "user", "content": "Санхүүгийн тайланг хэн өгөх вэ?"},
        {"role": "assistant", "content": "Болд [S3]."},
    ]
    await client.post("/api/ask", json={"question": "Хэзээ?", "history": history})
    msgs = fake.calls[0]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"].endswith("Асуулт: Хэзээ?")


@pytest.mark.asyncio
async def test_provider_error_returns_502(client: AsyncClient, monkeypatch):
    await _meeting_with_transcript(client, "Хурал")

    class _Broken(_FakeLLM):
        async def complete_json(self, messages):
            raise ask_mod.llm_mod.ProviderError("Qwen HTTP 429")

    monkeypatch.setattr(llm_mod, "llm_provider", _Broken({}))
    r = await client.post("/api/ask", json={"question": "юу?"})
    assert r.status_code == 502 and r.json()["detail"].startswith("ASK_FAILED")


@pytest.mark.asyncio
async def test_demo_provider_points_to_matching_segment(client: AsyncClient, monkeypatch):
    await _meeting_with_transcript(client, "Хурал")
    monkeypatch.setattr(llm_mod, "llm_provider", llm_mod.MockLLMProvider())
    data = (await client.post("/api/ask", json={"question": "Санхүүгийн тайлан"})).json()
    assert data["answer"].startswith("[ДЕМО]")
    assert data["citations"][0]["segment_id"] == "seg_3"


@pytest.mark.asyncio
async def test_no_meetings(client: AsyncClient):
    data = (await client.post("/api/ask", json={"question": "юу?"})).json()
    assert data["meetings_searched"] == 0 and data["citations"] == []


def test_long_transcripts_are_narrowed_to_relevant_segments(monkeypatch):
    from datetime import datetime, timezone

    filler = [ask_mod._Seg(f"S{i}", "m1", "Урт хурал", f"seg_{i}", i * 1000, i * 1000 + 999, "энгийн яриа " * 20) for i in range(200)]
    filler[150].text = "Маркетингийн төсвийг батлав"
    doc = ask_mod._MeetingDoc("m1", "Урт хурал", datetime.now(timezone.utc), "", filler)

    selected, partial = ask_mod._select([doc], "маркетингийн төсөв", budget=2000)
    assert partial
    texts = [s.segment_id for s in selected["m1"]]
    assert "seg_150" in texts and "seg_149" in texts and "seg_151" in texts  # хөршүүдтэй нь
    assert sum(len(ask_mod._seg_line(s)) + 1 for s in selected["m1"]) <= 2000
