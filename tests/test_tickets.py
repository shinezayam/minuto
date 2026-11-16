from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.main import app
from app.services import auth as auth_service
from tests.conftest import register


async def _meeting(client: AsyncClient) -> str:
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    await client.post(f"/api/meetings/{mid}/start")
    return mid


async def _ticket(client: AsyncClient, mid: str) -> str:
    r = await client.post(f"/api/meetings/{mid}/ws-ticket")
    assert r.status_code == 200
    assert r.json()["expires_in"] <= 60
    return r.json()["ticket"]


def _connect(mid: str, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return TestClient(app), f"/ws/meetings/{mid}/audio?{q}"


async def test_ws_ticket_connects_once_only(client: AsyncClient):
    mid = await _meeting(client)
    ticket = await _ticket(client, mid)
    c, url = _connect(mid, ticket=ticket)
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "recording.ready"
    # Нэг удаагийн: дахин ашиглавал татгалзана
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["code"] == "UNAUTHORIZED"


async def test_ws_ticket_is_scoped_to_its_meeting(client: AsyncClient):
    a, b = await _meeting(client), await _meeting(client)
    ticket = await _ticket(client, a)
    c, url = _connect(b, ticket=ticket)
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["code"] == "UNAUTHORIZED"


async def test_expired_and_forged_tickets_rejected(client: AsyncClient):
    mid = await _meeting(client)
    uid = (await client.get("/api/auth/me")).json()["id"]
    expired = jwt.encode(
        {"typ": "ticket", "scope": "ws", "sub": uid, "mid": mid, "ver": 0, "jti": "x", "exp": int(time.time()) - 5},
        auth_service._secret(), "HS256",
    )
    forged = jwt.encode(
        {"typ": "ticket", "scope": "ws", "sub": uid, "mid": mid, "ver": 0, "jti": "y", "exp": int(time.time()) + 60},
        "wrong-secret-0123456789abcdef0123456789", "HS256",
    )
    for bad in (expired, forged, "garbage"):
        c, url = _connect(mid, ticket=bad)
        with c, c.websocket_connect(url) as ws:
            assert ws.receive_json()["code"] == "UNAUTHORIZED"


async def test_access_token_and_ticket_are_not_interchangeable(client: AsyncClient):
    mid = await _meeting(client)
    ticket = await _ticket(client, mid)
    # тасалбарыг Bearer токен болгон ашиглах боломжгүй
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ticket}"})
    assert r.status_code == 401
    # access токеныг ?ticket= болгон ашиглах боломжгүй
    c, url = _connect(mid, ticket=client.auth_token)  # type: ignore[attr-defined]
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["code"] == "UNAUTHORIZED"


async def test_other_user_cannot_get_ticket(client: AsyncClient, anon_client: AsyncClient):
    mid = await _meeting(client)
    other = await register(anon_client)
    r = await anon_client.post(f"/api/meetings/{mid}/ws-ticket", headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 404


async def test_legacy_ws_query_token_follows_flag(client: AsyncClient, monkeypatch):
    mid = await _meeting(client)
    token = client.auth_token  # type: ignore[attr-defined]
    c, url = _connect(mid, token=token)
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "recording.ready"
    monkeypatch.setattr(settings, "allow_query_token", False)
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["code"] == "UNAUTHORIZED"


async def test_signed_audio_url(client: AsyncClient, anon_client: AsyncClient, tmp_path):
    mid = await _meeting(client)
    from app.services import processing

    d = processing.meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "uploaded.wav").write_bytes(b"RIFF....WAVEfmt ")
    r = await client.post(f"/api/meetings/{mid}/audio-url")
    body = r.json()
    assert body["url"].startswith(f"/api/meetings/{mid}/audio?sig=") and body["expires_in"] == settings.audio_url_ttl_sec

    anon = client.__class__(transport=client._transport, base_url="http://test")  # header-гүй клиент
    async with anon:
        ok = await anon.get(body["url"])
        assert ok.status_code == 200
        # өөр хурлын audio-д хүчингүй
        other = await _meeting(client)
        assert (await anon.get(f"/api/meetings/{other}/audio?sig={body['url'].split('sig=')[1]}")).status_code == 401
        # гарын үсэггүй, header-гүй
        assert (await anon.get(f"/api/meetings/{mid}/audio")).status_code == 401
        # ws тасалбарыг audio-д ашиглах боломжгүй
        wst = await _ticket(client, mid)
        assert (await anon.get(f"/api/meetings/{mid}/audio?sig={wst}")).status_code == 401


async def test_signed_audio_url_expires(client: AsyncClient, monkeypatch):
    mid = await _meeting(client)
    monkeypatch.setattr(settings, "audio_url_ttl_sec", -1)
    url = (await client.post(f"/api/meetings/{mid}/audio-url")).json()["url"]
    anon = client.__class__(transport=client._transport, base_url="http://test")
    async with anon:
        assert (await anon.get(url)).status_code == 401


async def test_logout_all_revokes_old_tokens_and_tickets(client: AsyncClient):
    mid = await _meeting(client)
    old_token = client.auth_token  # type: ignore[attr-defined]
    old_ticket = await _ticket(client, mid)
    r = await client.post("/api/auth/logout-all")
    assert r.status_code == 200
    new_token = r.json()["token"]
    # eski токен болон өмнө олгосон тасалбар хүчингүй
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})).status_code == 401
    c, url = _connect(mid, ticket=old_ticket)
    with c, c.websocket_connect(url) as ws:
        assert ws.receive_json()["code"] == "UNAUTHORIZED"
    # шинэ токен ажиллана
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})).status_code == 200


async def test_legacy_token_without_version_claim_still_valid(client: AsyncClient):
    uid = (await client.get("/api/auth/me")).json()["id"]
    legacy = jwt.encode({"sub": uid, "iat": int(time.time()), "exp": int(time.time()) + 600}, auth_service._secret(), "HS256")
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"})).status_code == 200
