"""ffmpeg-ээр аудиог задлах, хэсэглэх туслах функцууд."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_CHUNK_RE = re.compile(r"^\d{6}\.(webm|ogg|m4a|mp3|wav|bin)$")


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    if settings.ffmpeg_path:
        return settings.ffmpeg_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # dev орчинд: pip install imageio-ffmpeg
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as e:
        raise RuntimeError("ffmpeg олдсонгүй. ffmpeg суулгах эсвэл FFMPEG_PATH тохируулна уу.") from e


def find_meeting_audio(meeting_dir: Path) -> Optional[Path]:
    """
    Хурлын аудиог олно: upload хийсэн файл, эсвэл шууд бичлэгийн хэсгүүд.
    MediaRecorder-ийн хэсгүүд зөвхөн эхнийх нь header-тэй тул дангаараа задрахгүй,
    дарааллаар нь нийлүүлж нэг файл болгоно.
    """
    if not meeting_dir.is_dir():
        return None

    uploaded = sorted(meeting_dir.glob("uploaded*"))
    if uploaded:
        return uploaded[0]

    chunks = sorted(p for p in meeting_dir.iterdir() if _CHUNK_RE.match(p.name))
    if not chunks:
        return None

    ext = next((p.suffix for p in chunks if p.suffix != ".bin"), ".webm")
    combined = meeting_dir / f"recorded{ext}"
    # Нэг удаа нийлүүлээд кэшлэнэ: хэсгүүд өөрчлөгдөөгүй бол (нийт хэмжээ ижил) дахин бичихгүй
    expected = sum(p.stat().st_size for p in chunks)
    if combined.exists() and combined.stat().st_size == expected and combined.stat().st_mtime >= max(p.stat().st_mtime for p in chunks):
        return combined
    tmp = combined.with_suffix(combined.suffix + ".tmp")
    with tmp.open("wb") as out:
        for p in chunks:
            with p.open("rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)  # бүтэн хэсгийг санах ойд уншихгүй
    tmp.replace(combined)
    return combined


def probe_audio_error(path: Path) -> Optional[str]:
    """
    Файлыг ffmpeg-ээр нээж эхний секундийг задлана. Гэмтсэн/дутуу файл (жишээ нь moov atom-гүй m4a)
    бол алдааны мэдээг, хэвийн бол None буцаана. ffmpeg байхгүй бол шалгахгүй (None).
    """
    try:
        exe = ffmpeg_exe()
    except RuntimeError:
        return None
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-t", "1", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return proc.stderr.strip()[-300:] or f"ffmpeg exit {proc.returncode}"
    return None


def _split_sync(src: Path, out_dir: Path, chunk_sec: int, sample_rate: int) -> List[Path]:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
        "-f", "segment", "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
        str(out_dir / "part_%04d.wav"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise ValueError(f"Аудиог задалж чадсангүй: {proc.stderr.strip()[-500:]}")

    return sorted(out_dir.glob("part_*.wav"))


async def split_to_wav(src: Path, out_dir: Path, chunk_sec: int, sample_rate: int = 16000) -> List[Path]:
    """Аудиог mono 16kHz PCM wav болгож chunk_sec секундын хэсгүүдэд хуваана."""
    # Windows-ийн selector event loop дээр create_subprocess_exec ажилладаггүй тул thread-д ажиллуулна
    return await asyncio.to_thread(_split_sync, src, out_dir, chunk_sec, sample_rate)


def _run_ffmpeg(args: List[str], what: str) -> None:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        # ffmpeg-ийн stderr (зам, дотоод мэдээлэл) хэрэглэгчид харагдахгүй: зөвхөн лог руу
        logger.warning("ffmpeg %s failed: %s", what, proc.stderr.strip()[-500:])
        if "No space left" in proc.stderr:
            raise OSError(28, "No space left on device")
        raise ValueError("Аудиог задалж чадсангүй. Файл эвдэрсэн эсвэл дэмжигдээгүй формат байж магадгүй.")


def _to_flac_sync(src: Path, dst: Path, sample_rate: int) -> float:
    """Аудиог нэг удаа mono FLAC (lossless, WAV-аас ~3 дахин бага) болгоно. Буцаах: үргэлжлэх хугацаа (сек)."""
    tmp = dst.with_suffix(".tmp.flac")
    _run_ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "flac", str(tmp)], "to_flac")
    tmp.replace(dst)
    from app.services.upload_guard import probe_duration_sec  # ffmpeg-ийн Duration мөрийг уншина (FLAC-д найдвартай)

    seconds = probe_duration_sec(dst)
    if seconds is None:
        raise ValueError("Аудионы уртыг тодорхойлж чадсангүй.")
    return seconds


async def to_flac(src: Path, dst: Path, sample_rate: int = 16000) -> float:
    return await asyncio.to_thread(_to_flac_sync, src, dst, sample_rate)


def _extract_wav_sync(src: Path, start_sec: float, dur_sec: float, dst: Path, sample_rate: int) -> bytes:
    """[start, start+dur) хэсгийг PCM wav болгож уншаад, түр файлыг тэр даруй устгана."""
    try:
        _run_ffmpeg(["-ss", f"{start_sec:.3f}", "-t", f"{dur_sec:.3f}", "-i", str(src),
                     "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)], "extract")
        return dst.read_bytes()
    finally:
        dst.unlink(missing_ok=True)


async def extract_wav(src: Path, start_sec: float, dur_sec: float, dst: Path, sample_rate: int = 16000) -> bytes:
    return await asyncio.to_thread(_extract_wav_sync, src, start_sec, dur_sec, dst, sample_rate)


_DECODED_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def decoded_duration_sec(path: Path) -> Optional[float]:
    """Файлыг бүтэн decode хийж бодит уртыг хэмжинэ. Хөтчийн MediaRecorder-ийн webm (шууд бичлэг) толгойдоо урт
    бичдэггүй тул `Duration: N/A` гардаг — тэр үед ашиглана. Диск рүү юу ч бичихгүй (-f null)."""
    try:
        exe = ffmpeg_exe()
    except RuntimeError:
        return None
    proc = subprocess.run([exe, "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    matches = _DECODED_TIME.findall(proc.stderr)
    if not matches:
        return None
    h, m, s = matches[-1]
    seconds = int(h) * 3600 + int(m) * 60 + float(s)
    return seconds if seconds > 0 else None


def wav_bytes_duration_ms(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as w:
        return int(w.getnframes() * 1000 / w.getframerate())


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        return int(w.getnframes() * 1000 / w.getframerate())
