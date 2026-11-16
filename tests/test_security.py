from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.main import app
from app.security import SlidingWindow, client_ip, ws_origin_allowed
from app.services.auth import current_user

# Нэвтрэлтгүй байж болох route-ууд (бусад нь заавал current_user шаардана)
PUBLIC = {
    ("GET", "/healthz"),
    ("GET", "/api/meetings/{meeting_id}/audio"),  # өөрөө шалгадаг: header эсвэл гарын үсэгтэй ?sig=
    ("GET", "/api/health"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/google/config"),  # client ID нь нууц биш
    ("POST", "/api/auth/google"),  # Google ID token-оор өөрөө баталгаажна
    ("GET", "/api/openapi.json"),
    ("GET", "/api/docs"),
    ("GET", "/api/redoc"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/api/billing/plans"),  # нийтийн үнийн хүснэгт
    ("POST", "/api/billing/webhooks/wire"),  # Wire-ийн гарын үсгээр (HMAC) шалгагдана
    ("POST", "/api/contact-requests"),  # Enterprise хүсэлт: rate limit + validation
}
# WebSocket өөрөө токеныг шалгадаг (current_user dependency ашиглах боломжгүй)
SELF_AUTH_WS = {"/ws/meetings/{meeting_id}/audio"}


def _has_auth(route: APIRoute) -> bool:
    return any(d.call is current_user for d in route.dependant.dependencies)


def test_every_http_route_requires_auth_or_is_listed_public():
    unprotected = []
    for r in app.routes:
        if not hasattr(r, "methods"):
            continue
        for m in r.methods - {"HEAD", "OPTIONS"}:
            if (m, r.path) in PUBLIC:
                continue
            if not (isinstance(r, APIRoute) and _has_auth(r)):
                unprotected.append((m, r.path))
    assert not unprotected, f"Нэвтрэлтгүй route: {unprotected} — current_user нэмэх эсвэл PUBLIC-д зориуд оруулна уу"


def test_websocket_routes_are_known():
    ws = {r.path for r in app.routes if r.__class__.__name__ == "APIWebSocketRoute"}
    assert ws <= SELF_AUTH_WS, f"Шинэ WebSocket route: {ws - SELF_AUTH_WS}"


@pytest.mark.asyncio
async def test_anonymous_gets_401_on_protected_routes(anon_client: AsyncClient):
    for path in ("/api/meetings", "/api/templates", "/api/auth/me", "/api/workspaces", "/api/contacts"):
        assert (await anon_client.get(path)).status_code == 401, path


@pytest.mark.asyncio
async def test_other_user_gets_404_for_foreign_meeting(client: AsyncClient, anon_client: AsyncClient):
    from tests.conftest import register

    mid = (await client.post("/api/meetings", json={"title": "Нууц"})).json()["id"]
    other = await register(anon_client)
    h = {"Authorization": f"Bearer {other['token']}"}
    assert (await anon_client.get(f"/api/meetings/{mid}", headers=h)).status_code == 404
    assert (await anon_client.get(f"/api/meetings/{mid}/minutes", headers=h)).status_code == 404
    assert (await anon_client.delete(f"/api/meetings/{mid}", headers=h)).status_code == 404


@pytest.mark.asyncio
async def test_health_is_minimal(anon_client: AsyncClient):
    body = (await anon_client.get("/api/health")).json()
    assert set(body) == {"status", "version", "database", "live_transcription"}


@pytest.mark.asyncio
async def test_login_is_rate_limited(anon_client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_auth_per_min", 3)
    codes = [
        (await anon_client.post("/api/auth/login", json={"email": "a@b.mn", "password": "wrong-password"})).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]


@pytest.mark.asyncio
async def test_expensive_route_has_stricter_per_user_limit(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_expensive_per_min", 2)
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    codes = [(await client.post(f"/api/meetings/{mid}/process")).status_code for _ in range(4)]
    assert 429 in codes[2:]
    assert 429 not in codes[:2]


def test_sliding_window_is_bounded_and_expires():
    w = SlidingWindow()
    assert w.allow("b", "k", 2, now=0) and w.allow("b", "k", 2, now=1)
    assert not w.allow("b", "k", 2, now=2)
    assert w.allow("b", "k", 2, now=61)  # цонх өнгөрсний дараа дахин зөвшөөрнө
    for i in range(12_000):
        w.allow("b", f"ip{i}", 5, now=100 + i * 0.001)
    w._sweep(now=100.5)
    assert len(w.hits) <= 10_000


class _Req:
    def __init__(self, xff: str | None, host: str = "10.0.0.1"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_uses_last_forwarded_hop_only_behind_trusted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", False)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.delenv("RENDER", raising=False)
    assert client_ip(_Req("1.2.3.4, 5.6.7.8")) == "10.0.0.1"  # proxy-д найдахгүй бол header-ийг үл тоомсорлоно
    monkeypatch.setattr(settings, "trust_proxy", True)
    assert client_ip(_Req("6.6.6.6, 5.6.7.8")) == "5.6.7.8"  # эхнийх нь хуурамч байж болно, сүүлийнх нь proxy-ийн нэмсэн
    assert client_ip(_Req(None)) == "10.0.0.1"


def test_ws_origin_allow_list(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.mn")
    monkeypatch.setattr(settings, "ws_allowed_origins", "")
    assert ws_origin_allowed(None)  # хөтөч биш клиент: токенээр л нэвтэрнэ
    assert ws_origin_allowed("https://app.example.mn")
    assert not ws_origin_allowed("https://evil.example")


def test_websocket_rejects_foreign_origin(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.mn")
    monkeypatch.setattr(settings, "ws_allowed_origins", "")
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect) as ei:
            with c.websocket_connect("/ws/meetings/x/audio?token=t", headers={"origin": "https://evil.example"}):
                pass
        assert ei.value.code == 4403


def test_production_flag(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert settings.is_production and settings.proxy_trusted
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setenv("RENDER", "true")
    assert settings.is_production


def test_production_requires_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret", "")
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


async def test_cors_preflight_allows_if_match_for_editing(anon_client: AsyncClient, monkeypatch):
    """Frontend PUT-д If-Match явуулдаг: CORS preflight зөвшөөрөхгүй бол хөтчид "сервертэй холбогдож чадсангүй" гэж харагдана."""
    origin = settings.cors_origins.split(",")[0].strip()
    r = await anon_client.options(
        "/api/meetings/x/transcript",
        headers={"Origin": origin, "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "authorization,content-type,if-match"},
    )
    assert r.status_code == 200, r.text
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "if-match" in allowed and "authorization" in allowed
