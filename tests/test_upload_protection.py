from __future__ import annotations

import io
import json
import struct

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from app.config import settings
from app.main import app
from app.services import processing
from app.services import upload_guard as guard


def wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    n = int(seconds * rate)
    data = b"\x00\x00" * n
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )


async def _new_meeting(client: AsyncClient) -> str:
    return (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]


def _files(content: bytes, name="a.wav", ctype="audio/wav"):
    return {"file": (name, io.BytesIO(content), ctype)}


def test_sniff_audio_ext():
    assert guard.sniff_audio_ext(wav_bytes()[:16]) == ".wav"
    assert guard.sniff_audio_ext(b"ID3\x04" + b"\x00" * 12) == ".mp3"
    assert guard.sniff_audio_ext(b"OggS" + b"\x00" * 12) == ".ogg"
    assert guard.sniff_audio_ext(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 4) == ".m4a"
    assert guard.sniff_audio_ext(b"\x1a\x45\xdf\xa3" + b"\x00" * 12) == ".webm"
    assert guard.sniff_audio_ext(b"<html>not audio") is None
    assert guard.sniff_audio_ext(b"MZ\x90\x00 executable ") is None


async def test_valid_wav_upload_still_works(client: AsyncClient):
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes()))
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == "uploaded.wav"


async def test_oversized_upload_is_413_and_leaves_no_partial_file(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 10_000)
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes(seconds=2)))
    assert r.status_code == 413
    assert r.json()["detail"].startswith("FILE_TOO_LARGE")
    d = processing.meeting_dir(mid)
    assert not d.exists() or not any(d.iterdir())


async def test_spoofed_content_type_is_rejected(client: AsyncClient):
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(b"<html>hello this is not audio</html>", "a.mp3", "audio/mpeg"))
    assert r.status_code == 415
    assert r.json()["detail"].startswith("UNSUPPORTED_AUDIO")
    d = processing.meeting_dir(mid)
    assert not d.exists() or not any(d.iterdir())


async def test_non_audio_declared_type_is_rejected(client: AsyncClient):
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes(), "a.wav", "text/html"))
    assert r.status_code == 415


async def test_empty_upload_rejected(client: AsyncClient):
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(b""))
    assert r.status_code == 400


async def test_content_length_checked_early(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 1_000)
    mid = await _new_meeting(client)
    r = await client.post(
        f"/api/meetings/{mid}/upload", files=_files(wav_bytes()), headers={"content-length": "999999999"}
    )
    assert r.status_code == 413


async def test_user_quota_blocks_new_upload(client: AsyncClient, monkeypatch):
    mid = await _new_meeting(client)
    assert (await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes()))).status_code == 200
    monkeypatch.setattr(settings, "user_total_storage_bytes", 1_000)
    mid2 = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid2}/upload", files=_files(wav_bytes()))
    assert r.status_code == 413 and r.json()["detail"].startswith("QUOTA_EXCEEDED")


async def test_daily_quota(client: AsyncClient, monkeypatch):
    mid = await _new_meeting(client)
    assert (await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes()))).status_code == 200
    monkeypatch.setattr(settings, "user_daily_upload_bytes", 1_000)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes()))
    assert r.status_code == 429 and r.json()["detail"].startswith("DAILY_QUOTA_EXCEEDED")


async def test_disk_guard(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "min_free_disk_bytes", 10**18)
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes()))
    assert r.status_code == 507


async def test_too_long_recording_rejected(client: AsyncClient, monkeypatch):
    try:
        guard.ffmpeg_exe()
    except RuntimeError:
        pytest.skip("ffmpeg байхгүй")
    monkeypatch.setattr(settings, "max_recording_sec", 1)
    mid = await _new_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/upload", files=_files(wav_bytes(seconds=3)))
    assert r.status_code == 413 and r.json()["detail"].startswith("RECORDING_TOO_LONG")
    d = processing.meeting_dir(mid)
    assert not any(p.name.startswith("uploaded") for p in d.iterdir())


# ---------- WebSocket ----------

def _ws_session(client_token: str, meeting_id: str):
    c = TestClient(app)
    return c, c.websocket_connect(f"/ws/meetings/{meeting_id}/audio?token={client_token}")


def _recv_until(ws, types: set[str]):
    while True:
        m = ws.receive_json()
        if m.get("type") in types or m.get("code"):
            return m


async def _ws_setup(client: AsyncClient):
    mid = await _new_meeting(client)
    await client.post(f"/api/meetings/{mid}/start")
    return mid, client.auth_token  # type: ignore[attr-defined]


def _send_chunk(ws, seq, data=b"x" * 100, mime="audio/webm", ts=0):
    ws.send_text(json.dumps({"type": "audio.chunk", "sequence": seq, "mime_type": mime, "timestamp_ms": ts}))
    ws.send_bytes(data)


async def test_ws_total_size_cap_stops_recording(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "max_ws_total_bytes", 250)
    mid, token = await _ws_setup(client)
    with TestClient(app) as c, c.websocket_connect(f"/ws/meetings/{mid}/audio?token={token}") as ws:
        ws.receive_json()  # recording.ready
        _send_chunk(ws, 0)
        assert ws.receive_json()["type"] == "audio.ack"
        _send_chunk(ws, 1)
        assert ws.receive_json()["type"] == "audio.ack"
        _send_chunk(ws, 2)  # 300 > 250
        err = ws.receive_json()
        assert err["code"] == "RECORDING_TOO_LARGE"


async def test_ws_rejects_non_audio_mime_and_wild_sequence(client: AsyncClient):
    mid, token = await _ws_setup(client)
    with TestClient(app) as c, c.websocket_connect(f"/ws/meetings/{mid}/audio?token={token}") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "audio.chunk", "sequence": 0, "mime_type": "text/html", "timestamp_ms": 0}))
        assert ws.receive_json()["code"] == "UNSUPPORTED_MIME"
        ws.send_text(json.dumps({"type": "audio.chunk", "sequence": 5_000_000, "mime_type": "audio/webm", "timestamp_ms": 0}))
        assert ws.receive_json()["code"] == "BAD_SEQUENCE"
        ws.send_text(json.dumps({"type": "audio.chunk", "sequence": True, "mime_type": "audio/webm", "timestamp_ms": 0}))
        assert ws.receive_json()["code"] == "BAD_MESSAGE"


async def test_ws_duration_cap(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "max_recording_sec", 60)
    mid, token = await _ws_setup(client)
    with TestClient(app) as c, c.websocket_connect(f"/ws/meetings/{mid}/audio?token={token}") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "audio.chunk", "sequence": 0, "mime_type": "audio/webm", "timestamp_ms": 61_000}))
        assert ws.receive_json()["code"] == "RECORDING_TOO_LONG"


async def test_ws_refuses_when_disk_full(client: AsyncClient, monkeypatch):
    mid, token = await _ws_setup(client)
    monkeypatch.setattr(settings, "min_free_disk_bytes", 10**18)
    with TestClient(app) as c, c.websocket_connect(f"/ws/meetings/{mid}/audio?token={token}") as ws:
        assert ws.receive_json()["code"] == "STORAGE_FULL"


async def test_ws_keeps_recording_but_stops_live_transcript_when_minutes_run_out(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "billing_metering", "enforce")  # Free = 30 минут
    stopped = []

    async def fake_stop(meeting_id):
        stopped.append(meeting_id)

    from app.services import live_transcriber
    monkeypatch.setattr(live_transcriber, "stop", fake_stop)
    mid, token = await _ws_setup(client)
    with TestClient(app) as c, c.websocket_connect(f"/ws/meetings/{mid}/audio?token={token}") as ws:
        ws.receive_json()  # recording.ready
        _send_chunk(ws, 0, ts=26 * 60_000)  # 4 минут үлдлээ → анхааруулга
        low = _recv_until(ws, {"minutes.low"})
        assert low["type"] == "minutes.low" and low["remaining_ms"] == 4 * 60_000
        assert _recv_until(ws, {"audio.ack"})["sequence"] == 0
        _send_chunk(ws, 1, ts=30 * 60_000 + 3000)  # хэтэрлээ: бичлэг үргэлжилнэ
        assert _recv_until(ws, {"minutes.exhausted"})["type"] == "minutes.exhausted"
        assert _recv_until(ws, {"audio.ack"})["sequence"] == 1
        _send_chunk(ws, 2, ts=30 * 60_000 + 6000)
        assert _recv_until(ws, {"audio.ack"})["sequence"] == 2
    assert stopped == [mid]
    d = processing.meeting_dir(mid)
    assert sorted(p.name for p in d.iterdir() if p.name[:6].isdigit()) == ["000000.webm", "000001.webm", "000002.webm"]


async def test_ws_minute_limit_ignores_metering_off_and_superadmin(client: AsyncClient, monkeypatch):
    from app.services import billing

    mid, token = await _ws_setup(client)
    monkeypatch.setattr(settings, "billing_metering", "shadow")
    assert billing.recording_limit_ms(mid) is None
    monkeypatch.setattr(settings, "billing_metering", "enforce")
    assert billing.recording_limit_ms(mid) == 30 * 60_000
    me = (await client.get("/api/auth/me")).json()["email"]
    monkeypatch.setattr(settings, "admin_emails", me)
    assert billing.recording_limit_ms(mid) is None
