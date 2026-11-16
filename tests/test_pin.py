"""'Анхаарах' (pin) хурал: жагсаалтын дээр гарах, архивтай харьцах, эрх."""
from __future__ import annotations

from tests.conftest import register


async def _meeting(client, title: str) -> str:
    r = await client.post("/api/meetings", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_pinned_meetings_come_first_and_flags_are_exposed(client):
    old = await _meeting(client, "Хуучин")
    new = await _meeting(client, "Шинэ")
    r = await client.post(f"/api/meetings/{old}/pin")
    assert r.status_code == 200 and r.json()["pinned"] is True and r.json()["pinned_at"]

    rows = (await client.get("/api/meetings")).json()["meetings"]
    assert [m["id"] for m in rows] == [old, new]
    assert rows[0]["pinned"] is True and rows[1]["pinned"] is False
    assert all(m["archived"] is False for m in rows)

    assert (await client.post(f"/api/meetings/{old}/pin")).json()["pinned_at"] == r.json()["pinned_at"]  # idempotent
    assert (await client.post(f"/api/meetings/{old}/unpin")).json()["pinned"] is False
    rows = (await client.get("/api/meetings")).json()["meetings"]
    assert [m["id"] for m in rows] == [new, old]


async def test_archiving_unpins_and_archived_cannot_be_pinned(client):
    mid = await _meeting(client, "Т")
    await client.post(f"/api/meetings/{mid}/pin")
    archived = (await client.post(f"/api/meetings/{mid}/archive")).json()
    assert archived["archived"] is True and archived["pinned"] is False
    r = await client.post(f"/api/meetings/{mid}/pin")
    assert r.status_code == 409 and r.json()["detail"].startswith("ARCHIVED")
    rows = (await client.get("/api/meetings", params={"include_archived": True})).json()["meetings"]
    assert [m["archived"] for m in rows if m["id"] == mid] == [True]


async def test_other_users_cannot_pin(client, anon_client):
    mid = await _meeting(client, "Т")
    other = await register(anon_client)
    r = await anon_client.post(f"/api/meetings/{mid}/pin", headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 404
