"""ffmpeg-ээр аудиог задлах, хэсэглэх туслах функцууд."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from app.config import settings

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
    with combined.open("wb") as out:
        for p in chunks:
            out.write(p.read_bytes())
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


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        return int(w.getnframes() * 1000 / w.getframerate())
