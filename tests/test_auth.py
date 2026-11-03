"""Нэвтрэлт ба хэрэглэгч хоорондын тусгаарлалт."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.auth import hash_password, verify_password
from tests.conftest import register


def test_password_hash_roundtrip():
    h = hash_password("нууц үг 123")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("нууц үг 123", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "garbage")


async def test_register_login_me(anon_client):
    reg = await register(anon_client, "Bat@Test.mn")
    assert reg["user"]["email"] == "bat@test.mn"

    dup = await anon_client.post("/api/auth/register", json={"email": "bat@test.mn", "password": "password123"})
    assert dup.status_code == 409

    bad = await anon_client.post("/api/auth/login", json={"email": "bat@test.mn", "password": "wrongpass1"})
    assert bad.status_code == 401

    ok = await anon_client.post("/api/auth/login", json={"email": "BAT@test.mn", "password": "password123"})
    assert ok.status_code == 200
    me = await anon_client.get("/api/auth/me", headers={"Authorization": f"Bearer {ok.json()['token']}"})
    assert me.json()["email"] == "bat@test.mn"


@pytest.mark.parametrize("body", [{"email": "no-at", "password": "password123"}, {"email": "a@b.mn", "password": "short"}])
async def test_register_validation(anon_client, body):
    r = await anon_client.post("/api/auth/register", json=body)
    assert r.status_code == 422


async def test_registration_can_be_closed(anon_client, monkeypatch):
    monkeypatch.setattr(settings, "allow_registration", False)
    r = await anon_client.post("/api/auth/register", json={"email": "x@test.mn", "password": "password123"})
    assert r.status_code == 403


async def test_endpoints_require_auth(anon_client):
    assert (await anon_client.get("/api/meetings")).status_code == 401
    assert (await anon_client.post("/api/meetings", json={})).status_code == 401
    bad = await anon_client.get("/api/meetings", headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 401


async def test_users_cannot_see_each_others_meetings(anon_client):
    alice = await register(anon_client)
    bob = await register(anon_client)
    ha = {"Authorization": f"Bearer {alice['token']}"}
    hb = {"Authorization": f"Bearer {bob['token']}"}

    mid = (await anon_client.post("/api/meetings", json={"title": "Alice-ийн хурал"}, headers=ha)).json()["id"]

    assert len((await anon_client.get("/api/meetings", headers=ha)).json()["meetings"]) == 1
    assert (await anon_client.get("/api/meetings", headers=hb)).json()["meetings"] == []
    for method, path in [
        ("GET", f"/api/meetings/{mid}"),
        ("GET", f"/api/meetings/{mid}/transcript"),
        ("GET", f"/api/meetings/{mid}/audio"),
        ("POST", f"/api/meetings/{mid}/process"),
        ("DELETE", f"/api/meetings/{mid}"),
    ]:
        r = await anon_client.request(method, path, headers=hb)
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await anon_client.get(f"/api/meetings/{mid}", headers=ha)).status_code == 200


async def test_token_in_query_for_downloads(client):
    mid = (await client.post("/api/meetings", json={"title": "q"})).json()["id"]
    token = client.headers.pop("Authorization").split(" ", 1)[1]
    assert (await client.get(f"/api/meetings/{mid}")).status_code == 401
    assert (await client.get(f"/api/meetings/{mid}?token={token}")).status_code == 200


def test_database_url_normalization():
    from app.db import normalize_url

    assert normalize_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert normalize_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert normalize_url("sqlite:///./x.db") == "sqlite:///./x.db"
