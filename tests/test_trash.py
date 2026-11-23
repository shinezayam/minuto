"""Хогийн сав: төлбөртэй багцад устгасан хурал хогийн саванд, тэндээс бүрмөсөн устна. Free бол шууд устна."""
from __future__ import annotations

from datetime import timedelta

from app.config import settings
from app.db import Meeting, session_scope
from app.services import meeting_store, payments
from tests.conftest import register


async def _user(client, email, paid=False, monkeypatch=None):
    h = {"Authorization": f"Bearer {(await register(client, email))['token']}"}
    if paid:
        # Жинхэнэ төлбөрийн урсгал (FakeProvider): checkout → paid → багц идэвхжинэ
        fake = payments.FakeProvider()
        payments.set_provider(fake)
        monkeypatch.setattr(settings, "billing_checkout_enabled", True)
        try:
            r = await client.post("/api/billing/checkout", json={"plan_code": "starter"}, headers=h)
            assert r.status_code == 201, r.text
            fake.mark(next(iter(fake.intents)), "paid")
            assert (await client.get(f"/api/billing/orders/{r.json()['id']}", headers=h)).json()["status"] == "paid"
        finally:
            payments.set_provider(None)
    return h


async def _meeting(client, h, title="Хурал"):
    r = await client.post("/api/meetings", json={"title": title}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_free_user_delete_is_permanent(anon_client):
    h = await _user(anon_client, "free@minuto.mn")
    mid = await _meeting(anon_client, h)
    assert (await anon_client.delete(f"/api/meetings/{mid}", headers=h)).status_code == 204
    assert (await anon_client.get("/api/meetings/trash", headers=h)).json()["meetings"] == []
    with session_scope() as s:
        assert s.get(Meeting, mid) is None


async def test_paid_user_trash_restore_and_permanent_delete(anon_client, monkeypatch):
    h = await _user(anon_client, "paid@minuto.mn", paid=True, monkeypatch=monkeypatch)
    a, b = await _meeting(anon_client, h, "A"), await _meeting(anon_client, h, "B")

    assert (await anon_client.delete(f"/api/meetings/{a}", headers=h)).status_code == 204
    # Жагсаалт, дэлгэрэнгүйд харагдахгүй; хогийн саванд байна
    ids = [m["id"] for m in (await anon_client.get("/api/meetings", headers=h)).json()["meetings"]]
    assert a not in ids and b in ids
    assert (await anon_client.get(f"/api/meetings/{a}", headers=h)).status_code == 404
    trash = (await anon_client.get("/api/meetings/trash", headers=h)).json()["meetings"]
    assert [m["id"] for m in trash] == [a] and trash[0]["deleted_at"]

    # Сэргээх
    r = await anon_client.post(f"/api/meetings/{a}/restore", headers=h)
    assert r.status_code == 200 and r.json()["deleted_at"] is None
    assert (await anon_client.get(f"/api/meetings/{a}", headers=h)).status_code == 200

    # Хогийн сав → бүрмөсөн
    await anon_client.delete(f"/api/meetings/{a}", headers=h)
    assert (await anon_client.delete(f"/api/meetings/{a}", headers=h)).status_code == 204
    with session_scope() as s:
        assert s.get(Meeting, a) is None

    # Хоослох
    await anon_client.delete(f"/api/meetings/{b}", headers=h)
    assert (await anon_client.delete("/api/meetings/trash", headers=h)).status_code == 204
    assert (await anon_client.get("/api/meetings/trash", headers=h)).json()["meetings"] == []
    with session_scope() as s:
        assert s.get(Meeting, b) is None


async def test_other_users_cannot_see_or_restore_trash(anon_client, monkeypatch):
    h = await _user(anon_client, "owner@minuto.mn", paid=True, monkeypatch=monkeypatch)
    other = await _user(anon_client, "other@minuto.mn")
    mid = await _meeting(anon_client, h)
    await anon_client.delete(f"/api/meetings/{mid}", headers=h)
    assert (await anon_client.get("/api/meetings/trash", headers=other)).json()["meetings"] == []
    assert (await anon_client.post(f"/api/meetings/{mid}/restore", headers=other)).status_code == 404
    assert (await anon_client.delete(f"/api/meetings/{mid}", headers=other)).status_code == 404
    assert (await anon_client.post(f"/api/meetings/{mid}/restore", headers=anon_client.headers)).status_code == 401


async def test_old_trash_is_purged(anon_client, monkeypatch):
    h = await _user(anon_client, "old@minuto.mn", paid=True, monkeypatch=monkeypatch)
    mid = await _meeting(anon_client, h)
    await anon_client.delete(f"/api/meetings/{mid}", headers=h)
    assert meeting_store.purge_expired_trash() == []
    with session_scope() as s:
        m = s.get(Meeting, mid)
        m.deleted_at = m.deleted_at - timedelta(days=31)
    assert meeting_store.purge_expired_trash() == [mid]
