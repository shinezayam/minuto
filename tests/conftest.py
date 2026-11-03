from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import db
from app.config import settings
from app.main import app
from app.services import processing
from app.services.audio_ingest import audio_ingest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Тест бүр өөрийн SQLite болон аудио хавтастай."""
    storage = tmp_path / "audio"
    storage.mkdir()
    monkeypatch.setattr(settings, "audio_storage_path", str(storage))
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(audio_ingest, "_storage_root", storage)
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setattr(settings, "database_url", db_url)  # TestClient-ийн lifespan ч мөн үүнийг ашиглана
    db.configure(db_url)
    yield storage


async def register(client: AsyncClient, email: str | None = None) -> dict:
    email = email or f"user-{uuid.uuid4().hex[:8]}@test.mn"
    r = await client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return r.json()


@pytest_asyncio.fixture
async def anon_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await processing.wait_all()


@pytest_asyncio.fixture
async def client(anon_client):
    """Нэвтэрсэн хэрэглэгчийн client."""
    auth = await register(anon_client)
    anon_client.headers["Authorization"] = f"Bearer {auth['token']}"
    anon_client.auth_token = auth["token"]  # type: ignore[attr-defined]
    yield anon_client
