from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.db import Meeting, session_scope
from app.services import meeting_store
from app.services.transcript_edit import apply_edits

THREE_TURNS = {
    "id": "seg_1", "start_ms": 0, "end_ms": 30000,
    "text": "Бат: Сайн уу\nСараа: Сайн байна\nБат: Хурлаа эхэлье",
    "speaker": None,
    "turns": [
        {"speaker": "Бат", "text": "Сайн уу"},
        {"speaker": "Сараа", "text": "Сайн байна"},
        {"speaker": "Бат", "text": "Хурлаа эхэлье"},
    ],
}
SINGLE = {"id": "seg_2", "start_ms": 30000, "end_ms": 60000, "text": "Үнэ ярья", "speaker": None, "turns": []}
LABELLED_SINGLE = {
    "id": "seg_3", "start_ms": 60000, "end_ms": 90000, "text": "Дорж: Тайлан", "speaker": "Дорж",
    "turns": [{"speaker": "Дорж", "text": "Тайлан"}],
}


async def _meeting(client: AsyncClient, status: str | None = None) -> str:
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    import copy

    meeting_store.update_meeting_transcript(mid, copy.deepcopy([THREE_TURNS, SINGLE, LABELLED_SINGLE]))
    if status:
        with session_scope() as s:
            s.get(Meeting, mid).status = status
    return mid


async def _put(client, mid, segments, **kw):
    return await client.put(f"/api/meetings/{mid}/transcript", json={"segments": segments}, **kw)


async def _segs(client, mid):
    return {s["id"]: s for s in (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]}


# ---- F-20 regression ----

async def test_editing_one_turn_keeps_other_turns_and_speakers(client: AsyncClient):
    mid = await _meeting(client)
    r = await _put(client, mid, [{"id": "seg_1", "turn_index": 1, "text": "Сайн, сайн"}])
    assert r.status_code == 200, r.text
    seg = (await _segs(client, mid))["seg_1"]
    assert [(t["speaker"], t["text"]) for t in seg["turns"]] == [
        ("Бат", "Сайн уу"), ("Сараа", "Сайн, сайн"), ("Бат", "Хурлаа эхэлье"),
    ]
    assert seg["text"] == "Бат: Сайн уу\nСараа: Сайн, сайн\nБат: Хурлаа эхэлье"
    assert (seg["start_ms"], seg["end_ms"]) == (0, 30000)


async def test_speaker_edit_on_one_turn_only(client: AsyncClient):
    mid = await _meeting(client)
    await _put(client, mid, [{"id": "seg_1", "turn_index": 2, "speaker": "Дорж"}])
    seg = (await _segs(client, mid))["seg_1"]
    assert [t["speaker"] for t in seg["turns"]] == ["Бат", "Сараа", "Дорж"]


async def test_text_only_on_multi_turn_segment_is_rejected(client: AsyncClient):
    mid = await _meeting(client)
    before = await _segs(client, mid)
    for bad in ({"id": "seg_1", "text": "хэн нэгэн"}, {"id": "seg_1", "speaker": "Х"}):
        r = await _put(client, mid, [bad])
        assert r.status_code == 422 and r.json()["detail"].startswith("MULTI_TURN_SEGMENT")
    assert await _segs(client, mid) == before


async def test_full_turns_replacement(client: AsyncClient):
    mid = await _meeting(client)
    r = await _put(client, mid, [{"id": "seg_1", "turns": [{"speaker": "А", "text": "Нэг"}, {"speaker": "Б", "text": "Хоёр"}]}])
    assert r.status_code == 200
    seg = (await _segs(client, mid))["seg_1"]
    assert seg["text"] == "А: Нэг\nБ: Хоёр" and len(seg["turns"]) == 2


async def test_single_segment_and_single_turn_edits(client: AsyncClient):
    mid = await _meeting(client)
    await _put(client, mid, [{"id": "seg_2", "text": "Үнийг ярья", "speaker": "Бат"}, {"id": "seg_3", "text": "Тайлан бэлэн"}])
    segs = await _segs(client, mid)
    assert segs["seg_2"]["text"] == "Үнийг ярья" and segs["seg_2"]["speaker"] == "Бат"
    assert segs["seg_3"]["text"] == "Дорж: Тайлан бэлэн" and segs["seg_3"]["turns"][0]["text"] == "Тайлан бэлэн"


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "nope", "text": "x"},
        {"id": "seg_2", "text": "   "},
        {"id": "seg_1", "turn_index": 9, "text": "x"},
        {"id": "seg_2", "turn_index": 3, "text": "x"},
        {"id": "seg_1", "turns": [], },
        {"id": "seg_1", "turns": [{"speaker": "А", "text": " "}]},
        {"id": "seg_1", "turns": [{"text": "x"}], "text": "y"},
    ],
)
async def test_invalid_edits_rejected_atomically(client: AsyncClient, bad):
    mid = await _meeting(client)
    before = await _segs(client, mid)
    r = await _put(client, mid, [{"id": "seg_2", "text": "Зөв засвар"}, bad])
    assert r.status_code == 422, r.text
    assert await _segs(client, mid) == before  # эхний зөв засвар ч хэрэгжээгүй


def test_timings_cannot_be_changed_via_text_edit():
    out, _ = apply_edits([dict(SINGLE)], [{"id": "seg_2", "text": "шинэ"}])
    assert (out[0]["start_ms"], out[0]["end_ms"], out[0]["id"]) == (30000, 60000, "seg_2")


async def test_duplicate_segment_in_one_request_rejected(client: AsyncClient):
    mid = await _meeting(client)
    r = await _put(client, mid, [{"id": "seg_2", "text": "a"}, {"id": "seg_2", "text": "b"}])
    assert r.status_code == 422 and r.json()["detail"].startswith("DUPLICATE_SEGMENT")


# ---- concurrency ----

async def test_edit_blocked_while_processing(client: AsyncClient):
    mid = await _meeting(client, status="processing")
    r = await _put(client, mid, [{"id": "seg_2", "text": "x"}])
    assert r.status_code == 409 and r.json()["detail"].startswith("ALREADY_PROCESSING")


async def test_if_match_conflict_detected(client: AsyncClient):
    mid = await _meeting(client)
    etag = (await client.get(f"/api/meetings/{mid}/transcript")).json()["etag"]
    ok = await _put(client, mid, [{"id": "seg_2", "text": "A"}], headers={"If-Match": etag})
    assert ok.status_code == 200 and ok.json()["etag"] != etag
    stale = await _put(client, mid, [{"id": "seg_2", "text": "B"}], headers={"If-Match": etag})
    assert stale.status_code == 412 and stale.json()["detail"].startswith("CONFLICT")
    assert (await _segs(client, mid))["seg_2"]["text"] == "A"


async def test_other_user_gets_404(client: AsyncClient, anon_client: AsyncClient):
    from tests.conftest import register

    mid = await _meeting(client)
    other = await register(anon_client)
    h = {"Authorization": f"Bearer {other['token']}"}
    assert (await anon_client.put(f"/api/meetings/{mid}/transcript", json={"segments": [{"id": "seg_2", "text": "x"}]}, headers=h)).status_code == 404
    assert (await anon_client.get(f"/api/meetings/{mid}/transcript/revisions", headers=h)).status_code == 404


# ---- revisions ----

async def test_revision_saved_and_restorable(client: AsyncClient):
    mid = await _meeting(client)
    original = await _segs(client, mid)
    await _put(client, mid, [{"id": "seg_2", "text": "Засварласан"}])
    revs = (await client.get(f"/api/meetings/{mid}/transcript/revisions")).json()["revisions"]
    assert len(revs) == 1 and revs[0]["kind"] == "edit" and revs[0]["changed"] == ["seg_2"]

    r = await client.post(f"/api/meetings/{mid}/transcript/revisions/{revs[0]['id']}/restore")
    assert r.status_code == 200
    assert await _segs(client, mid) == original
    # сэргээлт өөрөө түүхэнд бичигдэж, буцаах боломжтой
    kinds = [x["kind"] for x in (await client.get(f"/api/meetings/{mid}/transcript/revisions")).json()["revisions"]]
    assert sorted(kinds) == ["edit", "restore"]


async def test_restore_unknown_or_foreign_revision_404(client: AsyncClient):
    a, b = await _meeting(client), await _meeting(client)
    await _put(client, a, [{"id": "seg_2", "text": "x"}])
    rid = (await client.get(f"/api/meetings/{a}/transcript/revisions")).json()["revisions"][0]["id"]
    assert (await client.post(f"/api/meetings/{b}/transcript/revisions/{rid}/restore")).status_code == 404
    assert (await client.post(f"/api/meetings/{a}/transcript/revisions/nope/restore")).status_code == 404


async def test_restore_blocked_while_processing(client: AsyncClient):
    mid = await _meeting(client)
    await _put(client, mid, [{"id": "seg_2", "text": "x"}])
    rid = (await client.get(f"/api/meetings/{mid}/transcript/revisions")).json()["revisions"][0]["id"]
    with session_scope() as s:
        s.get(Meeting, mid).status = "processing"
    assert (await client.post(f"/api/meetings/{mid}/transcript/revisions/{rid}/restore")).status_code == 409


async def test_revisions_are_capped(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(meeting_store, "MAX_REVISIONS", 3)
    mid = await _meeting(client)
    for i in range(6):
        await _put(client, mid, [{"id": "seg_2", "text": f"v{i}"}])
    assert len((await client.get(f"/api/meetings/{mid}/transcript/revisions")).json()["revisions"]) == 3


async def test_replace_and_reprocess_keep_a_revision(client: AsyncClient):
    mid = await _meeting(client)
    r = await client.post(f"/api/meetings/{mid}/transcript/replace", json={"find": "Үнэ", "replace": "Үнийг"})
    assert r.status_code == 200
    kinds = [x["kind"] for x in (await client.get(f"/api/meetings/{mid}/transcript/revisions")).json()["revisions"]]
    assert kinds == ["replace"]
    meeting_store.update_meeting_transcript(mid, [], snapshot_kind="reprocess")  # боловсруулалт шинэ бичвэр бичихэд
    kinds = [x["kind"] for x in (await client.get(f"/api/meetings/{mid}/transcript/revisions")).json()["revisions"]]
    assert set(kinds) == {"reprocess", "replace"}
