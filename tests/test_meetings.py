from __future__ import annotations

import pytest
from httpx import AsyncClient

# `client` fixture (нэвтэрсэн хэрэглэгч) tests/conftest.py-д байна


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_create_and_get_meeting(client: AsyncClient):
    resp = await client.post("/api/meetings", json={"title": "Тест хурал"})
    assert resp.status_code == 201
    meeting = resp.json()
    assert meeting["title"] == "Тест хурал"
    assert meeting["status"] == "draft"
    meeting_id = meeting["id"]

    resp2 = await client.get(f"/api/meetings/{meeting_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == meeting_id


@pytest.mark.asyncio
async def test_meeting_lifecycle(client: AsyncClient):
    resp = await client.post("/api/meetings", json={"title": "Lifecycle test"})
    meeting_id = resp.json()["id"]

    resp = await client.post(f"/api/meetings/{meeting_id}/start")
    assert resp.json()["status"] == "recording"

    resp = await client.post(f"/api/meetings/{meeting_id}/pause")
    assert resp.json()["status"] == "paused"

    resp = await client.post(f"/api/meetings/{meeting_id}/resume")
    assert resp.json()["status"] == "recording"

    resp = await client.post(f"/api/meetings/{meeting_id}/end")
    assert resp.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_list_meetings(client: AsyncClient):
    await client.post("/api/meetings", json={"title": "List test 1"})
    await client.post("/api/meetings", json={"title": "List test 2"})
    resp = await client.get("/api/meetings")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["meetings"]) >= 2


@pytest.mark.asyncio
async def test_update_meeting_title(client: AsyncClient):
    resp = await client.post("/api/meetings", json={"title": "Old title"})
    meeting_id = resp.json()["id"]

    resp = await client.patch(f"/api/meetings/{meeting_id}", json={"title": "Шинэ гарчиг"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Шинэ гарчиг"


@pytest.mark.asyncio
async def test_delete_meeting(client: AsyncClient):
    resp = await client.post("/api/meetings", json={"title": "Delete me"})
    meeting_id = resp.json()["id"]

    resp = await client.delete(f"/api/meetings/{meeting_id}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/meetings/{meeting_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_meeting_not_found(client: AsyncClient):
    resp = await client.get("/api/meetings/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_audio(client: AsyncClient):
    # Create a meeting first
    resp = await client.post("/api/meetings", json={"title": "Upload test"})
    meeting_id = resp.json()["id"]

    # Upload a real (silent) WAV file
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    files = {"file": ("test.wav", io.BytesIO(buf.getvalue()), "audio/wav")}
    resp = await client.post(f"/api/meetings/{meeting_id}/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["meeting_id"] == meeting_id
    assert data["status"] == "uploaded"
    assert "filename" in data


@pytest.mark.asyncio
async def test_upload_rejects_corrupt_audio(client: AsyncClient):
    from app.services.audio_utils import ffmpeg_exe
    try:
        ffmpeg_exe()
    except RuntimeError:
        pytest.skip("ffmpeg алга")

    import io
    meeting_id = (await client.post("/api/meetings", json={"title": "Corrupt"})).json()["id"]
    # moov atom-гүй, дутуу m4a
    files = {"file": ("broken.m4a", io.BytesIO(b"\x00\x00\x00\x18ftypM4A broken data"), "audio/m4a")}
    resp = await client.post(f"/api/meetings/{meeting_id}/upload", files=files)
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("INVALID_AUDIO")
    # Гэмтсэн файл хадгалагдахгүй тул Process "аудио алга" гэж хэлнэ
    resp = await client.post(f"/api/meetings/{meeting_id}/process")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_health_reports_database(client: AsyncClient):
    assert (await client.get("/api/health")).json()["database"] == "sqlite"
