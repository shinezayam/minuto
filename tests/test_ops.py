from __future__ import annotations

import json
import logging

from httpx import AsyncClient

from app.openapi_snapshot import SNAPSHOT, current


def test_openapi_contract_snapshot_is_up_to_date():
    """API-ийн бүтэц өөрчлөгдсөн бол `python -m app.openapi_snapshot` ажиллуулж docs/openapi.json-г commit хийнэ
    (frontend-ийн хүлээж буй contract-ыг ухамсартайгаар шинэчлэхийн тулд)."""
    assert SNAPSHOT.read_text(encoding="utf-8") == current(), "OpenAPI өөрчлөгдсөн: python -m app.openapi_snapshot"


async def test_healthz_is_public_and_minimal(anon_client: AsyncClient):
    r = await anon_client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_readyz_requires_auth_and_reports(client: AsyncClient):
    async with client.__class__(transport=client._transport, base_url="http://test") as anon:  # header-гүй
        assert (await anon.get("/api/readyz")).status_code == 401
    body = (await client.get("/api/readyz")).json()
    assert body["status"] in {"ready", "degraded"}
    assert body["checks"]["database"]["ok"] is True and body["checks"]["storage"]["ok"] is True
    assert "qwen_api_key" not in json.dumps(body).lower()


async def test_request_id_header_and_access_log_has_no_secrets(client: AsyncClient, caplog):
    caplog.set_level(logging.INFO, logger="minuto.access")
    r = await client.get("/api/meetings?token=SECRET-TOKEN-VALUE&sig=SECRET-SIG", headers={"x-request-id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "minuto.access"]
    assert lines, "access log бичигдсэнгүй"
    joined = " ".join(lines)
    assert "SECRET" not in joined and "token" not in joined and "Bearer" not in joined
    entry = json.loads(lines[-1])
    assert entry["request_id"] == "abc-123" and entry["path"] == "/api/meetings" and entry["status"] == 200


async def test_hostile_request_id_is_sanitised(anon_client: AsyncClient):
    r = await anon_client.get("/healthz", headers={"x-request-id": "a b\r\nX-Evil: 1"})
    assert "\n" not in r.headers["x-request-id"] and " " not in r.headers["x-request-id"]
