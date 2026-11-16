"""Аудиог байнга (deploy, restart-аас хамааралгүй) хадгалах давхарга.

Загвар: сервер дээрх диск = ажлын КЭШ (ASR, тоглуулалт, ffmpeg файл хэрэгтэй), удаан хадгалалт = Remote (Supabase Storage).
  * persist(meeting_id)      — хурлын эцсийн аудио файлыг (upload эсвэл нийлүүлсэн бичлэг) remote руу хуулна
  * ensure_local(meeting_id) — дискэнд аудио алга бол remote-оос татна (dual-read: эхлээд local)
  * purge(meeting_id)        — local + remote устгана; remote амжилтгүй бол лог бичиж, дахин оролдоно
AUDIO_STORAGE_BACKEND=local үед (default) энэ бүхэн no-op: өмнөх үйл ажиллагаа өөрчлөгдөхгүй.
Шууд бичлэгийн хэсгүүд (chunk) зөвхөн дискэнд; бичлэг дууссаны дараа нэг файл болж remote руу орно.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
import shutil
import threading
from pathlib import Path
from typing import Dict, List, Optional, Protocol
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.audio_utils import find_meeting_audio

logger = logging.getLogger(__name__)

_AUDIO_KEYS = ("audio",)  # remote дээрх нэг хурлын эцсийн аудио: "<meeting_id>/audio<.ext>"


class RemoteStore(Protocol):
    def put_file(self, key: str, path: Path, content_type: str) -> None: ...
    def get_file(self, key: str, dest: Path) -> None: ...
    def list(self, prefix: str) -> List[str]: ...
    def delete(self, keys: List[str]) -> None: ...


class StorageUnavailable(Exception):
    """Remote хадгалалт хариу өгсөнгүй / татгалзлаа."""


class SupabaseStore:
    """Supabase Storage REST (private bucket). service_role түлхүүр зөвхөн серверт, header-ээр л явна."""

    def __init__(self, url: str, key: str, bucket: str, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self._headers = {"Authorization": f"Bearer {key}", "apikey": key}
        self._client = httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0), transport=transport)

    def _obj(self, key: str) -> str:
        return f"{self.base}/object/{quote(self.bucket)}/{quote(key, safe='/')}"

    def _check(self, r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            # Хариун биеийг (түлхүүр, нэр агуулж болзошгүй) бүтнээр нь бүү лог бич
            raise StorageUnavailable(f"{what}: HTTP {r.status_code}")

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        try:
            with path.open("rb") as f:
                r = self._client.post(
                    self._obj(key), content=f, headers={**self._headers, "content-type": content_type, "x-upsert": "true"}
                )
        except httpx.HTTPError as e:
            raise StorageUnavailable(f"upload: {type(e).__name__}") from e
        self._check(r, "upload")

    def get_file(self, key: str, dest: Path) -> None:
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._client.stream("GET", self._obj(key), headers=self._headers) as r:
                self._check(r, "download")
                with tmp.open("wb") as out:
                    for chunk in r.iter_bytes(1024 * 1024):
                        out.write(chunk)
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise StorageUnavailable(f"download: {type(e).__name__}") from e
        except StorageUnavailable:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)

    def list(self, prefix: str) -> List[str]:
        try:
            r = self._client.post(
                f"{self.base}/object/list/{quote(self.bucket)}",
                json={"prefix": prefix.rstrip("/"), "limit": 1000},
                headers=self._headers,
            )
        except httpx.HTTPError as e:
            raise StorageUnavailable(f"list: {type(e).__name__}") from e
        self._check(r, "list")
        return [f"{prefix.rstrip('/')}/{o['name']}" for o in r.json() if o.get("name")]

    def sizes(self, prefix: str) -> Dict[str, int]:
        """Объект бүрийн хэмжээ (байт): list хариуны metadata.size. Хуулсны дараа шалгахад ашиглана."""
        try:
            r = self._client.post(
                f"{self.base}/object/list/{quote(self.bucket)}",
                json={"prefix": prefix.rstrip("/"), "limit": 1000},
                headers=self._headers,
            )
        except httpx.HTTPError as e:
            raise StorageUnavailable(f"list: {type(e).__name__}") from e
        self._check(r, "list")
        out: Dict[str, int] = {}
        for o in r.json():
            size = (o.get("metadata") or {}).get("size")
            if o.get("name") and isinstance(size, int):
                out[f"{prefix.rstrip('/')}/{o['name']}"] = size
        return out

    def delete(self, keys: List[str]) -> None:
        if not keys:
            return
        try:
            r = self._client.request("DELETE", f"{self.base}/object/{quote(self.bucket)}", json={"prefixes": keys}, headers=self._headers)
        except httpx.HTTPError as e:
            raise StorageUnavailable(f"delete: {type(e).__name__}") from e
        self._check(r, "delete")


_store: Optional[RemoteStore] = None
_store_ready = False
_lock = threading.Lock()


def _make_store() -> Optional[RemoteStore]:
    if settings.audio_storage_backend == "local":
        return None
    if settings.audio_storage_backend != "supabase":
        raise RuntimeError(f"Тодорхойгүй AUDIO_STORAGE_BACKEND: {settings.audio_storage_backend}")
    if not (settings.supabase_url and settings.supabase_service_role_key and settings.supabase_audio_bucket):
        raise RuntimeError("AUDIO_STORAGE_BACKEND=supabase боловч SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_AUDIO_BUCKET дутуу")
    return SupabaseStore(settings.supabase_url, settings.supabase_service_role_key, settings.supabase_audio_bucket)


def get_store() -> Optional[RemoteStore]:
    global _store, _store_ready
    if not _store_ready:
        _store, _store_ready = _make_store(), True
    return _store


def set_store(store: Optional[RemoteStore]) -> None:
    """Тест / тусгай тохиргоонд."""
    global _store, _store_ready
    _store, _store_ready = store, True


def validate_config() -> None:
    """Эхлэхдээ: буруу тохиргоо бол шууд унана (production-д нууцаар local руу унахгүй)."""
    get_store()


def enabled() -> bool:
    return get_store() is not None


def _content_type(path: Path) -> str:
    return {
        ".webm": "audio/webm", ".ogg": "audio/ogg", ".wav": "audio/wav", ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4", ".flac": "audio/flac", ".aac": "audio/aac",
    }.get(path.suffix.lower(), "application/octet-stream")


def _dir(meeting_id: str) -> Path:
    return Path(settings.audio_storage_path) / meeting_id


def _remote_audio(store: RemoteStore, meeting_id: str) -> Optional[str]:
    names = [k for k in store.list(meeting_id) if k.rsplit("/", 1)[-1].startswith("audio")]
    return names[0] if names else None


def persist(meeting_id: str) -> bool:
    """Эцсийн аудиог remote руу хуулна. Local backend бол юу ч хийхгүй (True). Алдаа: StorageUnavailable."""
    store = get_store()
    if store is None:
        return True
    audio = find_meeting_audio(_dir(meeting_id))
    if audio is None:
        return False
    key = f"{meeting_id}/audio{audio.suffix}"
    existing = _remote_audio(store, meeting_id)
    if existing and existing != key:
        store.delete([existing])  # өөр өргөтгөлтэй хуучин хувилбар
    local_size = audio.stat().st_size
    store.put_file(key, audio, _content_type(audio))
    sizes = getattr(store, "sizes", None)
    if sizes is not None:
        remote_size = sizes(meeting_id).get(key)
        if remote_size != local_size:
            # Дутуу хуулагдсан (сүлжээ таслагдсан, хэмжээний хязгаар): найдвартай хадгалагдсан гэж үзэхгүй
            logger.error("Audio persist size mismatch for meeting %s: local %d, remote %s", meeting_id, local_size, remote_size)
            raise StorageUnavailable("upload: size mismatch")
    logger.info("Audio persisted for meeting %s (%d bytes, verified)", meeting_id, local_size)
    return True


def ensure_local(meeting_id: str) -> bool:
    """Дискэнд аудио байвал True. Үгүй бол remote-оос татаж `uploaded.<ext>` болгон хадгална. Алга бол False."""
    d = _dir(meeting_id)
    if find_meeting_audio(d) is not None:
        return True
    store = get_store()
    if store is None:
        return False
    key = _remote_audio(store, meeting_id)
    if not key:
        return False
    d.mkdir(parents=True, exist_ok=True)
    ext = Path(key).suffix or ".bin"
    store.get_file(key, d / f"uploaded{ext}")
    logger.info("Audio restored from storage for meeting %s", meeting_id)
    return True


def purge(meeting_id: str) -> None:
    """Local + remote устгана. Remote амжилтгүй бол ХЭЗЭЭ Ч чимээгүй орхихгүй: DB-д бүртгэж, дараа дахин оролдоно
    (сервер дахин эхэлсэн ч дараалал хадгалагдана)."""
    shutil.rmtree(_dir(meeting_id), ignore_errors=True)
    store = get_store()
    if store is None:
        return
    from app.db import AudioPurge, session_scope

    with session_scope() as s:
        if s.get(AudioPurge, meeting_id) is None:
            s.add(AudioPurge(meeting_id=meeting_id, attempts=0))
    retry_pending_purges([meeting_id])


def retry_pending_purges(only: Optional[List[str]] = None) -> int:
    """Хүлээгдэж буй remote устгалуудыг дахин оролдоно (10 мин тутам + шинэ устгал бүрд). Үлдсэн тоог буцаана."""
    store = get_store()
    if store is None:
        return 0
    from sqlalchemy import func, select

    from app.db import AudioPurge, session_scope

    with _lock:
        with session_scope() as s:
            q = select(AudioPurge.meeting_id).order_by(AudioPurge.created_at).limit(100)
            if only is not None:
                q = q.where(AudioPurge.meeting_id.in_(only))
            todo = list(s.scalars(q).all())
        for mid in todo:
            try:
                store.delete(store.list(mid))
            except StorageUnavailable as e:
                with session_scope() as s:
                    row = s.get(AudioPurge, mid)
                    if row is not None:
                        row.attempts += 1
                        row.last_error = str(e)[:200]
                        row.updated_at = datetime.now(timezone.utc)
                        attempts = row.attempts
                logger.error("Remote audio purge for meeting %s failed (attempt %d): %s", mid, attempts, e)
            else:
                with session_scope() as s:
                    row = s.get(AudioPurge, mid)
                    if row is not None:
                        s.delete(row)
        with session_scope() as s:
            return int(s.scalar(select(func.count()).select_from(AudioPurge)) or 0)


def pending_purges() -> Dict[str, int]:
    """meeting_id → дахин оролдсон тоо (readyz-д)."""
    if get_store() is None:
        return {}
    from sqlalchemy import select

    from app.db import AudioPurge, session_scope

    with session_scope() as s:
        return {r.meeting_id: r.attempts for r in s.scalars(select(AudioPurge)).all()}
