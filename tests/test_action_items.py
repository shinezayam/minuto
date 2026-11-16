from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.services import meeting_store


async def _meeting_with_minutes(client: AsyncClient, items=None, segments=("seg_1", "seg_2")) -> str:
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    meeting_store.update_meeting_transcript(
        mid, [{"id": s, "start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": "x"} for i, s in enumerate(segments)]
    )
    meeting_store.update_meeting_minutes(
        mid, {"title": "Х", "summary": "С", "action_items": items if items is not None else [{"task": "Тайлан", "owner": "Бат", "due_date": "Баасан", "status": "open"}]}
    )
    return mid


async def _put(client, mid, items, **kw):
    return await client.put(f"/api/meetings/{mid}/action-items", json={"action_items": items}, **kw)


async def test_valid_update_is_saved_and_normalised(client: AsyncClient):
    mid = await _meeting_with_minutes(client)
    r = await _put(client, mid, [{"task": "  Шинэ даалгавар  ", "owner": " Сараа ", "due_date": "2026-12-01", "status": "in_progress", "source_segment_ids": ["seg_1", "seg_99"]}])
    assert r.status_code == 200, r.text
    item = r.json()["action_items"][0]
    assert item["task"] == "Шинэ даалгавар" and item["owner"] == "Сараа"
    assert item["source_segment_ids"] == ["seg_1"]  # байхгүй сегмент хасагдсан
    assert (await client.get(f"/api/meetings/{mid}/minutes")).json()["action_items"][0]["status"] == "in_progress"


@pytest.mark.parametrize(
    "bad",
    [
        {"task": ""},
        {"task": "   "},
        {"task": "x" * 501},
        {"task": "ok", "owner": "o" * 101},
        {"task": "ok", "status": "finished"},
        {"task": "ok", "due_date": "маргааш"},
        {"task": "ok", "due_date": "2026-13-40"},
        {"task": "ok", "source_segment_ids": ["s"] * 51},
        {"owner": "Бат"},
    ],
)
async def test_malformed_items_are_422(client: AsyncClient, bad):
    mid = await _meeting_with_minutes(client)
    assert (await _put(client, mid, [bad])).status_code == 422


async def test_too_many_items_and_wrong_shape_are_422(client: AsyncClient):
    mid = await _meeting_with_minutes(client)
    assert (await _put(client, mid, [{"task": "t"}] * 201)).status_code == 422
    assert (await client.put(f"/api/meetings/{mid}/action-items", json={"action_items": "nope"})).status_code == 422
    assert (await client.put(f"/api/meetings/{mid}/action-items", json={})).status_code == 422


async def test_unchanged_legacy_free_text_due_date_round_trips(client: AsyncClient):
    """Хуучин минутын "Баасан" гэх due_date-тэй даалгаврын статусыг солиход 422 өгөхгүй."""
    mid = await _meeting_with_minutes(client)
    items = (await client.get(f"/api/meetings/{mid}/minutes")).json()["action_items"]
    items[0]["status"] = "done"
    r = await _put(client, mid, items)
    assert r.status_code == 200 and r.json()["action_items"][0]["due_date"] == "Баасан"
    # харин шинэ чөлөөт текстийг татгалзана
    items[0]["due_date"] = "маргааш"
    assert (await _put(client, mid, items)).status_code == 422


async def test_legacy_minutes_still_load_and_export(client: AsyncClient):
    mid = await _meeting_with_minutes(client, items=[{"task": "Хуучин", "owner": None, "due_date": "дараа сар", "status": "pending"}])
    assert (await client.get(f"/api/meetings/{mid}/minutes")).status_code == 200
    assert (await client.get(f"/api/meetings/{mid}/export.docx")).status_code == 200


async def test_if_match_conflict(client: AsyncClient):
    mid = await _meeting_with_minutes(client)
    first = await _put(client, mid, [{"task": "A"}])
    etag = first.json()["etag"]
    ok = await _put(client, mid, [{"task": "B"}], headers={"If-Match": etag})
    assert ok.status_code == 200
    stale = await _put(client, mid, [{"task": "C"}], headers={"If-Match": etag})  # etag эскирсэн
    assert stale.status_code == 412 and stale.json()["detail"].startswith("CONFLICT")
    assert (await client.get(f"/api/meetings/{mid}/minutes")).json()["action_items"][0]["task"] == "B"


async def test_no_minutes_is_404(client: AsyncClient):
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    assert (await _put(client, mid, [{"task": "A"}])).status_code == 404


async def test_other_user_cannot_edit(client: AsyncClient, anon_client: AsyncClient):
    from tests.conftest import register

    mid = await _meeting_with_minutes(client)
    other = await register(anon_client)
    r = await anon_client.put(
        f"/api/meetings/{mid}/action-items", json={"action_items": [{"task": "x"}]}, headers={"Authorization": f"Bearer {other['token']}"}
    )
    assert r.status_code == 404


async def test_legacy_status_values_are_mapped(client: AsyncClient):
    mid = await _meeting_with_minutes(client)
    r = await _put(client, mid, [{"task": "A", "status": "pending"}, {"task": "B", "status": "completed"}])
    assert [i["status"] for i in r.json()["action_items"]] == ["open", "done"]
