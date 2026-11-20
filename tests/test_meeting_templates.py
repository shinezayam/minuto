from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient

from app.models.minutes import MeetingMinutes, TranscriptSegment
from app.services import llm as llm_mod

from tests.conftest import register

SEG = [TranscriptSegment(id="seg_1", start_ms=0, end_ms=1000, text="Сайн байна уу")]


async def _default_template(client: AsyncClient) -> dict:
    r = await client.get("/api/templates")
    assert r.status_code == 200
    return next(t for t in r.json() if t["name"] == "Daily Standup")


async def test_create_meeting_with_template(client: AsyncClient):
    tmpl = await _default_template(client)
    r = await client.post("/api/meetings", json={"title": "Т", "template_id": tmpl["id"]})
    assert r.status_code == 201
    assert r.json()["template_id"] == tmpl["id"]
    assert (await client.get(f"/api/meetings/{r.json()['id']}")).json()["template_id"] == tmpl["id"]


async def test_meeting_without_template_is_null(client: AsyncClient):
    r = await client.post("/api/meetings", json={"title": "Т"})
    assert r.json()["template_id"] is None


async def test_unknown_template_rejected(client: AsyncClient):
    r = await client.post("/api/meetings", json={"title": "Т", "template_id": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"].startswith("TEMPLATE_NOT_FOUND")


async def test_other_users_template_rejected(client: AsyncClient, anon_client: AsyncClient):
    mine = await client.post("/api/templates", json={"name": "Миний", "icon": "📋", "sections": ["summary"]})
    assert mine.status_code in (200, 201), mine.text
    other = await register(anon_client)
    r = await anon_client.post(
        "/api/meetings",
        json={"title": "Т", "template_id": mine.json()["id"]},
        headers={"Authorization": f"Bearer {other['token']}"},
    )
    assert r.status_code == 404


def test_apply_template_sections_keeps_all_groups_and_orders():
    minutes = MeetingMinutes.model_validate(
        {
            "title": "Х",
            "summary": "Т",
            "topics": [{"title": "a", "description": "b"}],
            "decisions": [{"text": "d"}],
            "action_items": [{"task": "t"}],
            "open_questions": [{"text": "q"}],
        }
    )
    out = llm_mod.apply_template_sections(minutes, {"sections": ["summary", "action_items"]})
    # хэсгийг хоослохгүй, зөвхөн онцлох дарааллыг тэмдэглэнэ
    assert len(out.topics) == len(out.decisions) == len(out.open_questions) == len(out.action_items) == 1
    assert out.section_order == ["action_items"]
    # загваргүй бол онцлох хэсэг байхгүй
    plain = MeetingMinutes.model_validate({"title": "Х", "summary": "Т"})
    assert llm_mod.apply_template_sections(plain, None).section_order == []


def test_language_instruction():
    assert llm_mod.language_instruction("mn") == ""
    assert "English" in llm_mod.language_instruction("en")


async def test_qwen_prompt_carries_template_and_language(monkeypatch):
    monkeypatch.setattr(llm_mod.settings, "qwen_api_key", "sk-test")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        content = json.dumps({"title": "Х", "summary": "Т", "topics": [{"title": "a", "description": "b"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        llm_mod.httpx,
        "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler), **{x: y for x, y in k.items() if x != "transport"}),
    )
    tmpl = {"name": "Daily Standup", "sections": ["summary", "action_items"], "custom_prompt": "Богино бич"}
    m = await llm_mod.QwenLLMProvider().generate_minutes(SEG, None, template=tmpl, language="en")
    system, user = (x["content"] for x in seen["body"]["messages"])
    assert "English" in system
    assert "Daily Standup" in user and "Богино бич" in user
    assert len(m.topics) == 1  # загварт ороогүй ч хоослохгүй
    assert m.section_order == ["action_items"]


async def test_default_templates_not_duplicated(client: AsyncClient):
    import asyncio

    results = await asyncio.gather(*(client.get("/api/templates") for _ in range(5)))
    names = [t["name"] for t in results[-1].json() if t["is_default"]]
    assert len(names) == len(set(names)) == 6


async def test_existing_duplicate_defaults_are_merged(client: AsyncClient):
    from app.db import Meeting, MeetingTemplate, session_scope
    from app.services import templates as tmpl_service

    first = next(t for t in (await client.get("/api/templates")).json() if t["name"] == "Daily Standup")
    with session_scope() as s:
        dup = MeetingTemplate(name="Daily Standup", icon="⚡", sections=["summary"], is_default=1)
        s.add(dup)
        s.flush()
        dup_id = dup.id
    m = (await client.post("/api/meetings", json={"title": "Т", "template_id": dup_id})).json()

    tmpl_service.ensure_default_templates()

    standups = [t for t in (await client.get("/api/templates")).json() if t["name"] == "Daily Standup"]
    assert [t["id"] for t in standups] == [first["id"]]
    assert (await client.get(f"/api/meetings/{m['id']}")).json()["template_id"] == first["id"]
