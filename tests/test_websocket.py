"""Шууд бичлэгийн WebSocket — хэсэг хүлээн авах, ack, дахин илгээлт, буруу дараалал."""
from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.audio_utils import ffmpeg_exe, find_meeting_audio, split_to_wav

from tests.test_providers import needs_ffmpeg


class _AuthedClient:
    """TestClient + токен: REST хүсэлтэд header, WebSocket-д ?token= нэмнэ."""

    def __init__(self, tc: TestClient, token: str):
        self.tc, self.token = tc, token
        tc.headers["Authorization"] = f"Bearer {token}"

    def post(self, *a, **kw):
        return self.tc.post(*a, **kw)

    def websocket_connect(self, path: str, token: str | None = "default"):
        tok = self.token if token == "default" else token
        return self.tc.websocket_connect(path + (f"?token={tok}" if tok else ""))


def _signup(tc: TestClient, email: str) -> str:
    r = tc.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


@pytest.fixture
def client(isolated_env):
    with TestClient(app) as tc:
        yield _AuthedClient(tc, _signup(tc, "ws@test.mn"))


def _meeting(client) -> str:
    return client.post("/api/meetings", json={"title": "ws"}).json()["id"]


def _meta(seq: int) -> str:
    return json.dumps({"type": "audio.chunk", "sequence": seq, "mime_type": "audio/webm;codecs=opus", "timestamp_ms": seq * 3000})


def test_chunks_are_acked_and_stored(client, isolated_env):
    mid = _meeting(client)
    with client.websocket_connect(f"/ws/meetings/{mid}/audio") as ws:
        assert ws.receive_json()["type"] == "recording.ready"
        for seq in range(3):
            ws.send_text(_meta(seq))
            ws.send_bytes(bytes([seq]) * 10)
            ack = ws.receive_json()
            assert ack == {"type": "audio.ack", "sequence": seq, "meeting_id": mid}

    files = sorted(p.name for p in (isolated_env / mid).iterdir())
    assert files == ["000000.webm", "000001.webm", "000002.webm"]


def test_resend_after_reconnect_is_idempotent(client, isolated_env):
    mid = _meeting(client)
    for _ in range(2):  # ижил хэсгийг хоёр холболтоор илгээнэ
        with client.websocket_connect(f"/ws/meetings/{mid}/audio") as ws:
            ws.receive_json()
            ws.send_text(_meta(0))
            ws.send_bytes(b"abc")
            assert ws.receive_json()["sequence"] == 0
    assert (isolated_env / mid / "000000.webm").read_bytes() == b"abc"


def test_protocol_errors_do_not_kill_connection(client):
    mid = _meeting(client)
    with client.websocket_connect(f"/ws/meetings/{mid}/audio") as ws:
        ws.receive_json()
        ws.send_bytes(b"orphan")  # meta-гүй аудио
        assert ws.receive_json()["code"] == "CHUNK_WITHOUT_META"
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
        # meta, meta, audio — эхнийх нь алгасагдаж, хоёр дахь нь хадгалагдана
        ws.send_text(_meta(0))
        ws.send_text(_meta(1))
        ws.send_bytes(b"x")
        assert ws.receive_json() == {"type": "audio.ack", "sequence": 1, "meeting_id": mid}


def test_unknown_meeting_rejected(client):
    with client.websocket_connect("/ws/meetings/nope/audio") as ws:
        assert ws.receive_json()["code"] == "MEETING_NOT_FOUND"


def test_websocket_requires_token(client):
    mid = _meeting(client)
    for token in (None, "garbage"):
        with client.websocket_connect(f"/ws/meetings/{mid}/audio", token=token) as ws:
            assert ws.receive_json()["code"] == "UNAUTHORIZED"


def test_websocket_rejects_other_users_meeting(client):
    mid = _meeting(client)
    other = _signup(client.tc, "other@test.mn")
    with client.websocket_connect(f"/ws/meetings/{mid}/audio", token=other) as ws:
        assert ws.receive_json()["code"] == "MEETING_NOT_FOUND"


@needs_ffmpeg
async def test_sliced_webm_stream_is_decodable(tmp_path):
    """MediaRecorder шиг нэг үргэлжилсэн webm/opus урсгалыг дурын байтаар хуваасан хэсгүүдийг нийлүүлж задлах."""
    src = tmp_path / "rec.webm"
    subprocess.run(
        [ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=12",
         "-c:a", "libopus", "-b:a", "32k", str(src)],
        check=True,
    )
    blob = src.read_bytes()
    meeting_dir = tmp_path / "m"
    meeting_dir.mkdir()
    step = len(blob) // 4 + 1
    for i in range(0, len(blob), step):
        (meeting_dir / f"{i // step:06d}.webm").write_bytes(blob[i:i + step])

    combined = find_meeting_audio(meeting_dir)
    assert combined.read_bytes() == blob
    parts = await split_to_wav(combined, tmp_path / "parts", chunk_sec=5)
    assert len(parts) == 3
