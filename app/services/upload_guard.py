"""Аудио upload / шууд бичлэгийн хамгаалалт: хэмжээ, формат, урт, квот, дискний хомсдол."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from app.config import settings
from app.services.audio_utils import ffmpeg_exe


class GuardError(Exception):
    """HTTP/WS хоёуланд хэрэглэгдэх бүтэцтэй алдаа: code, status, хэрэглэгчид харуулах мессеж."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.message}"


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.0f} MB"


# Файлын эхний байтаар форматыг тодорхойлно (Content-Type хуурамч байж болно)
def sniff_audio_ext(head: bytes) -> Optional[str]:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        # ADTS aac: FFF1/FFF9; mp3 frame: FFFB/FFF3/FFE3...
        if len(head) > 1 and head[0] == 0xFF and head[1] in (0xF1, 0xF9):
            return ".aac"
        return ".mp3"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"  # EBML: webm / mkv
    if head[4:8] == b"ftyp":
        return ".m4a"  # mp4 / m4a / mov
    if head[:6] == b"#!AMR\n" or head[:9] == b"#!AMR-WB\n":
        return ".amr"
    return None


def declared_type_ok(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.startswith("audio/") or ct.startswith("video/") or ct == "application/octet-stream"


def free_disk_ok() -> None:
    root = Path(settings.audio_storage_path)
    root.mkdir(parents=True, exist_ok=True)
    if settings.min_free_disk_bytes and shutil.disk_usage(root).free < settings.min_free_disk_bytes:
        raise GuardError("STORAGE_FULL", 507, "Серверийн хадгалах зай дүүрсэн байна. Түр хүлээгээд дахин оролдоно уу.")


def dir_bytes(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


def user_usage(meeting_ids: list[str]) -> tuple[int, int]:
    """(нийт байт, өнөөдөр бичигдсэн байт) — хэрэглэгчийн хурлуудын аудио хавтсуудаас."""
    root = Path(settings.audio_storage_path)
    cutoff = time.time() - 86400
    total = today = 0
    for mid in meeting_ids:
        d = root / mid
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                st = p.stat()
                total += st.st_size
                if st.st_mtime >= cutoff:
                    today += st.st_size
    return total, today


def check_quota(meeting_ids: list[str], incoming: int = 0) -> None:
    total, today = user_usage(meeting_ids)
    if settings.user_total_storage_bytes and total + incoming > settings.user_total_storage_bytes:
        raise GuardError("QUOTA_EXCEEDED", 413, f"Таны хадгалах хязгаар ({_mb(settings.user_total_storage_bytes)}) дүүрсэн байна. Хуучин хурлын аудиог устгана уу.")
    if settings.user_daily_upload_bytes and today + incoming > settings.user_daily_upload_bytes:
        raise GuardError("DAILY_QUOTA_EXCEEDED", 429, "Өнөөдрийн аудио оруулах хязгаар хэтэрлээ. Маргааш дахин оролдоно уу.")


def check_declared_size(content_length: Optional[str]) -> None:
    if settings.max_upload_bytes and content_length and content_length.isdigit():
        # multipart хүрээний тохируулга бага зэрэг нэмэгдэнэ
        if int(content_length) > settings.max_upload_bytes + 100_000:
            raise too_large()


def too_large() -> GuardError:
    return GuardError("FILE_TOO_LARGE", 413, f"Файл хэт том байна (дээд хэмжээ {_mb(settings.max_upload_bytes)}).")


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration_sec(path: Path) -> Optional[float]:
    try:
        exe = ffmpeg_exe()
    except RuntimeError:
        return None
    proc = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = _DURATION.search(proc.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def check_duration(seconds: Optional[float]) -> None:
    if settings.max_recording_sec and seconds is not None and seconds > settings.max_recording_sec:
        raise GuardError("RECORDING_TOO_LONG", 413, f"Бичлэг хэт урт байна (дээд хугацаа {settings.max_recording_sec // 3600} цаг).")
