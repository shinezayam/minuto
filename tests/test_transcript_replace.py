from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.services import meeting_store
from app.services.text_replace import build_pattern, replace_in_obj, replace_in_segments

SEGMENTS = [
    # turns-тэй хэсэг: segment.text-д "Яригч: " шошго орсон байдаг, тоололд орохгүй
    {"id": "s1", "start_ms": 0, "end_ms": 1000, "text": "Бат: Бат хэлэв. бат зөв. Батын санал.", "speaker": "Бат",
     "turns": [{"speaker": "Бат", "text": "Бат хэлэв. бат зөв. Батын санал."}]},
    {"id": "s2", "start_ms": 1000, "end_ms": 2000, "text": "Дорж зөвшөөрөв.", "speaker": None, "turns": []},
]


def test_whole_word_case_insensitive():
    segs, n = replace_in_segments(SEGMENTS, build_pattern("Бат", True, False), "Баатар")
    assert n == 2
    assert segs[0]["text"] == "Баатар: Баатар хэлэв. Баатар зөв. Батын санал."
    assert segs[0]["speaker"] == "Баатар"
    assert segs[0]["turns"][0] == {"speaker": "Баатар", "text": "Баатар хэлэв. Баатар зөв. Батын санал."}
    assert SEGMENTS[0]["text"].startswith("Бат: Бат хэлэв")  # эх өгөгдөл өөрчлөгдөхгүй


def test_partial_and_case_sensitive():
    segs, n = replace_in_segments(SEGMENTS, build_pattern("Бат", False, True), "Баатар")
    assert n == 2  # "бат" (жижиг) тоологдохгүй, "Батын" тоологдоно
    assert segs[0]["turns"][0]["text"] == "Баатар хэлэв. бат зөв. Баатарын санал."


def test_replacement_is_literal():
    segs, _ = replace_in_segments(SEGMENTS, build_pattern("Дорж", True, False), r"\1 $&")
    assert segs[1]["text"] == r"\1 $& зөвшөөрөв."


def test_minutes_skip_ids():
    minutes = {"summary": "Бат ярив", "topics": [{"title": "Бат", "source_segment_ids": ["Бат"]}]}
    out, n = replace_in_obj(minutes, build_pattern("Бат", True, False), "Баатар")
    assert n == 2
    assert out["topics"][0]["source_segment_ids"] == ["Бат"]


@pytest.mark.asyncio
async def test_replace_endpoint(client: AsyncClient):
    meeting_id = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    meeting_store.update_meeting_transcript(meeting_id, SEGMENTS)
    meeting_store.update_meeting_minutes(meeting_id, {"title": "Бат", "summary": "Бат ярив"})

    r = await client.post(f"/api/meetings/{meeting_id}/transcript/replace", json={"find": "бат", "replace": "Баатар"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["replaced"] == 2 and data["minutes_replaced"] == 2
    assert data["segments"][0]["turns"][0]["text"].startswith("Баатар хэлэв")

    t = (await client.get(f"/api/meetings/{meeting_id}/transcript")).json()
    assert t["segments"][0]["text"] == data["segments"][0]["text"]
    assert meeting_store.get_meeting_minutes(meeting_id)["summary"] == "Баатар ярив"

    r = await client.post(f"/api/meetings/{meeting_id}/transcript/replace", json={"find": " ", "replace": "x"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_replace_without_minutes_and_busy(client: AsyncClient):
    meeting_id = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    meeting_store.update_meeting_transcript(meeting_id, SEGMENTS)
    meeting_store.update_meeting_minutes(meeting_id, {"summary": "Дорж"})
    r = await client.post(
        f"/api/meetings/{meeting_id}/transcript/replace",
        json={"find": "Дорж", "replace": "Доржоо", "include_minutes": False},
    )
    assert r.json()["replaced"] == 1 and r.json()["minutes_replaced"] == 0
    assert meeting_store.get_meeting_minutes(meeting_id)["summary"] == "Дорж"

    assert meeting_store.try_begin_processing(meeting_id)
    r = await client.post(f"/api/meetings/{meeting_id}/transcript/replace", json={"find": "a", "replace": "b"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_replace_other_users_meeting(client: AsyncClient, anon_client: AsyncClient):
    meeting_id = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    from tests.conftest import register
    other = await register(anon_client)
    r = await anon_client.post(
        f"/api/meetings/{meeting_id}/transcript/replace",
        json={"find": "a", "replace": "b"},
        headers={"Authorization": f"Bearer {other['token']}"},
    )
    assert r.status_code == 404
