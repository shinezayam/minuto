from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from app.config import settings
from app.services import audio_store, processing
from app.services.audio_store import StorageUnavailable, SupabaseStore
from tests.test_upload_protection import wav_bytes


class FakeStore:
    """Санах ойн remote: put/get/list/delete."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_delete = 0
        self.fail_put = False

    def put_file(self, key, path, content_type):
        if self.fail_put:
            raise StorageUnavailable("upload: HTTP 500")
        self.objects[key] = Path(path).read_bytes()

    def get_file(self, key, dest):
        Path(dest).write_bytes(self.objects[key])

    def list(self, prefix):
        return [k for k in self.objects if k.startswith(prefix.rstrip("/") + "/")]

    def delete(self, keys):
        if self.fail_delete:
            self.fail_delete -= 1
            raise StorageUnavailable("delete: HTTP 500")
        for k in keys:
            self.objects.pop(k, None)


@pytest.fixture
def remote():
    fake = FakeStore()
    audio_store.set_store(fake)
    yield fake
    audio_store.set_store(None)
    audio_store._pending_purges.clear()


async def _meeting(client: AsyncClient) -> str:
    return (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]


def _upload(client, mid, content=None):
    return client.post(f"/api/meetings/{mid}/upload", files={"file": ("a.wav", content or wav_bytes(), "audio/wav")})


async def test_local_backend_is_a_noop(client: AsyncClient):
    audio_store.set_store(None)
    assert not audio_store.enabled()
    mid = await _meeting(client)
    assert (await _upload(client, mid)).status_code == 200
    assert audio_store.persist(mid) is True and audio_store.ensure_local(mid) is True


async def test_upload_is_persisted_and_survives_disk_loss(client: AsyncClient, remote: FakeStore):
    mid = await _meeting(client)
    wav = wav_bytes(seconds=1)
    assert (await _upload(client, mid, wav)).status_code == 200
    assert remote.objects[f"{mid}/audio.wav"] == wav

    shutil.rmtree(processing.meeting_dir(mid))  # Render deploy: диск арилна
    r = await client.get(f"/api/meetings/{mid}/audio")  # remote-оос сэргээгдэнэ
    assert r.status_code == 200 and r.content == wav


async def test_processing_restores_audio_after_disk_loss(client: AsyncClient, remote: FakeStore):
    mid = await _meeting(client)
    await _upload(client, mid)
    shutil.rmtree(processing.meeting_dir(mid))
    r = await client.post(f"/api/meetings/{mid}/process")
    assert r.status_code == 202, r.text
    await processing.wait_all()
    assert (await client.get(f"/api/meetings/{mid}")).json()["processing_error"] != "NO_AUDIO"


async def test_upload_fails_loudly_when_storage_is_down(client: AsyncClient, remote: FakeStore):
    remote.fail_put = True
    mid = await _meeting(client)
    r = await _upload(client, mid)
    assert r.status_code == 503 and r.json()["detail"].startswith("STORAGE_UNAVAILABLE")
    d = processing.meeting_dir(mid)
    assert not any(p.name.startswith("uploaded") for p in d.iterdir())  # "хадгалагдлаа" гэж хуурахгүй


async def test_live_recording_chunks_are_consolidated_and_persisted_on_process(client: AsyncClient, remote: FakeStore):
    mid = await _meeting(client)
    d = processing.meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "000000.webm").write_bytes(b"AAAA")
    (d / "000001.webm").write_bytes(b"BBBB")
    r = await client.post(f"/api/meetings/{mid}/process")
    assert r.status_code == 202
    await processing.wait_all()
    assert remote.objects[f"{mid}/audio.webm"] == b"AAAABBBB"


async def test_delete_removes_local_and_remote(client: AsyncClient, remote: FakeStore):
    mid = await _meeting(client)
    await _upload(client, mid)
    assert remote.objects
    assert (await client.delete(f"/api/meetings/{mid}")).status_code == 204
    assert not remote.objects and not processing.meeting_dir(mid).exists()


async def test_failed_remote_delete_is_logged_and_retried(client: AsyncClient, remote: FakeStore, caplog):
    mid = await _meeting(client)
    await _upload(client, mid)
    remote.fail_delete = 1
    assert (await client.delete(f"/api/meetings/{mid}")).status_code == 204
    assert mid in audio_store.pending_purges() and remote.objects  # орхигдоогүй
    assert any("purge" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)
    assert audio_store.retry_pending_purges() == 0  # дараагийн оролдлого амжилттай
    assert not remote.objects


def test_chunk_concat_is_streamed_and_cached(tmp_path):
    from app.services.audio_utils import find_meeting_audio

    d = tmp_path / "m"
    d.mkdir()
    (d / "000000.webm").write_bytes(b"1111")
    (d / "000001.webm").write_bytes(b"2222")
    first = find_meeting_audio(d)
    assert first.read_bytes() == b"11112222"
    mtime = first.stat().st_mtime_ns
    assert find_meeting_audio(d).stat().st_mtime_ns == mtime  # дахин бичээгүй
    (d / "000002.webm").write_bytes(b"3333")
    assert find_meeting_audio(d).read_bytes() == b"111122223333"  # шинэ хэсэг орвол дахин нийлүүлнэ


def test_invalid_backend_config_fails_fast(monkeypatch):
    monkeypatch.setattr(settings, "audio_storage_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", "")
    audio_store._store_ready = False
    with pytest.raises(RuntimeError, match="SUPABASE"):
        audio_store.validate_config()
    monkeypatch.setattr(settings, "audio_storage_backend", "s3")
    audio_store._store_ready = False
    with pytest.raises(RuntimeError, match="Тодорхойгүй"):
        audio_store.validate_config()
    audio_store.set_store(None)


# ---------- Supabase REST ----------

def _supabase(handler) -> SupabaseStore:
    return SupabaseStore("https://x.supabase.co/", "SERVICE-KEY", "meeting-audio-private", transport=httpx.MockTransport(handler))


def test_supabase_upload_download_list_delete(tmp_path):
    seen: list[httpx.Request] = []
    blobs: dict[str, bytes] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if req.method == "POST" and path.startswith("/storage/v1/object/list/"):
            return httpx.Response(200, json=[{"name": "audio.wav"}])
        if req.method == "POST":
            blobs[path] = req.content
            return httpx.Response(200, json={"Key": "ok"})
        if req.method == "GET":
            return httpx.Response(200, content=blobs[path])
        if req.method == "DELETE":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    s = _supabase(handler)
    src = tmp_path / "a.wav"
    src.write_bytes(b"RIFFdata")
    s.put_file("mid/audio.wav", src, "audio/wav")
    dest = tmp_path / "out.wav"
    s.get_file("mid/audio.wav", dest)
    assert dest.read_bytes() == b"RIFFdata"
    assert s.list("mid") == ["mid/audio.wav"]
    s.delete(["mid/audio.wav"])

    up = seen[0]
    assert up.url.path == "/storage/v1/object/meeting-audio-private/mid/audio.wav"
    assert up.headers["authorization"] == "Bearer SERVICE-KEY" and up.headers["x-upsert"] == "true"
    delete_body = json.loads([r for r in seen if r.method == "DELETE"][0].content)
    assert delete_body == {"prefixes": ["mid/audio.wav"]}


def test_supabase_errors_are_typed_and_do_not_leak_body(tmp_path):
    s = _supabase(lambda req: httpx.Response(403, text="secret details SERVICE-KEY"))
    src = tmp_path / "a.wav"
    src.write_bytes(b"x")
    with pytest.raises(StorageUnavailable) as ei:
        s.put_file("k", src, "audio/wav")
    assert "SERVICE-KEY" not in str(ei.value) and "403" in str(ei.value)
    with pytest.raises(StorageUnavailable):
        s.get_file("k", tmp_path / "d.wav")
    assert not list(tmp_path.glob("d.wav*"))  # хагас файл үлдээхгүй


def test_supabase_network_error_is_typed(tmp_path):
    def boom(req):
        raise httpx.ConnectError("down")

    s = _supabase(boom)
    with pytest.raises(StorageUnavailable):
        s.list("m")
