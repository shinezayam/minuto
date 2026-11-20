"""
Хурлын үеэр бичвэрийг шууд гаргана.

Шууд бичлэгийн хэсгүүд (MediaRecorder-ийн webm/ogg) нэг үргэлжилсэн урсгал тул тэдгээрийг
дарааллаар нь нэг ffmpeg процесс руу хийж, 16kHz mono PCM болгон уншина. PCM-ийг чимээгүй
хэсэг дээр (үгийг таслахгүйн тулд) 10-25 секундын цонхонд хувааж ASR руу илгээгээд,
гарсан бичвэрийг тухайн хурлын WebSocket холболтууд руу "transcript.segment" болгон түгээнэ.

Текст болгосон (эсвэл чимээгүй гэж алгассан) цонх бүрийг live_windows.jsonl-д хадгална. Хурал дууссаны дараа
processing.run() эдгээрийг дахин ASR-д явуулахгүй, зөвхөн хамрагдаагүй завсрыг (алдаа гарсан цонх, сүүлийн хэдэн
секунд) текст болгоно. Ингэснээр шууд бичлэгийн аудиог хоёр удаа текст болгохгүй.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import subprocess
import threading
import time
import wave
from array import array
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set

from app.config import settings
from app.services import asr as asr_mod
from app.services import vocabulary
from app.services.audio_utils import ffmpeg_exe

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 100
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2  # s16le
# Энэ RMS-ээс доош бол чимээгүй гэж үзнэ (int16 хуваарьт)
SILENCE_RMS = 300
# Цонхны дундаж RMS үүнээс доош бол ASR руу илгээхгүй (Omni чимээгүй дээр зохиож бичихээс сэргийлнэ)
MIN_SPEECH_RMS = 150
# ffmpeg-ээр урсгалаар задлах боломжтой формат (Safari-гийн mp4 хэсгүүд урсгалаар задрахгүй)
STREAMABLE_EXT = {".webm", ".ogg"}
IDLE_TIMEOUT_SEC = 15 * 60

Listener = Callable[[dict], Awaitable[None]]

# Хурлын үеэр текст болгосон цонхнууд (мөр бүр {"start_ms","end_ms","text"}; text нь толь бичгийн засваргүй)
WINDOWS_FILE = "live_windows.jsonl"


def load_windows(meeting_dir: Path) -> List[dict]:
    """Хадгалсан цонхнуудыг уншина. Файл байхгүй, эсвэл гэмтсэн мөрийг алгасна."""
    path = meeting_dir / WINDOWS_FILE
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            w = json.loads(line)
            out.append({"start_ms": int(w["start_ms"]), "end_ms": int(w["end_ms"]), "text": str(w.get("text") or "")})
        except (ValueError, KeyError, TypeError):
            continue
    return out


def discard_windows(meeting_dir: Path) -> None:
    """Аудио солигдсон (файл upload хийсэн) бол хуучин шууд бичвэр тэр аудиод хамаарахгүй."""
    (meeting_dir / WINDOWS_FILE).unlink(missing_ok=True)


def _rms(pcm: bytes) -> float:
    samples = array("h", pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def choose_cut(frame_rms: List[float], min_frames: int, target_frames: int, max_frames: int) -> Optional[int]:
    """
    Хэдэн frame-ийн дараа таслахыг сонгоно. Хангалттай урт болоогүй бол None.
    target-аас хойш min..одоогийн хооронд хамгийн чимээгүй frame-ийг хайж, тэр нь чимээгүй
    эсвэл max-д хүрсэн бол тэнд таслана.
    """
    n = len(frame_rms)
    if n < target_frames:
        return None
    end = min(n, max_frames)
    window = frame_rms[min_frames:end]
    if not window:
        return end
    idx = min(range(len(window)), key=window.__getitem__)
    if window[idx] < SILENCE_RMS or n >= max_frames:
        return min_frames + idx + 1
    return None


class LiveTranscriber:
    def __init__(self, meeting_id: str, meeting_dir: Path) -> None:
        self.meeting_id = meeting_id
        self.meeting_dir = meeting_dir
        self.segments: List[dict] = []
        self.listeners: Set[Listener] = set()
        self.disabled_reason: Optional[str] = None

        self._next_seq = 0
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pcm = bytearray()  # сүүлийн таслалтаас хойших PCM
        self._frame_rms: List[float] = []  # _pcm-ийн бүтэн frame бүрийн RMS
        self._consumed_samples = 0  # таслагдаж ASR руу явсан нийт sample (цаг тооцоход)
        self._feed_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._flush_requested = False
        self._closed = False
        self._last_activity = time.monotonic()
        self._task: Optional[asyncio.Task] = None
        self._asr_context = asr_mod.NO_CONTEXT
        self._corrections: List[tuple[str, str]] = []

        # Сервер дахин эхэлсэн, эсвэл сул байгаад хаагдсан бол: өмнө текст болгосон хэсгийг дахин ASR-д явуулахгүй.
        # Хэсгүүдийг ffmpeg-д эхнээс нь хийнэ (webm-ийн header эхний хэсэгт), гэхдээ хамрагдсан PCM-ийг хаяна.
        done = load_windows(meeting_dir)
        covered_ms = max((w["end_ms"] for w in done), default=0)
        self._consumed_samples = covered_ms * SAMPLE_RATE // 1000
        self._skip_bytes = self._consumed_samples * 2
        for w in done:
            if w["text"].strip():
                self.segments.append({
                    "type": "transcript.segment", "segment_id": f"live_{len(self.segments) + 1}",
                    "start_ms": w["start_ms"], "end_ms": w["end_ms"], "text": w["text"], "status": "provisional",
                })

    # ---------- аудио оруулах ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name=f"live:{self.meeting_id}")

    async def notify_new_audio(self) -> None:
        """Шинэ хэсэг дискэнд бичигдсэн. Дараалал тасраагүй хэсгүүдийг ffmpeg руу хийнэ."""
        if self._closed or self.disabled_reason:
            return
        async with self._feed_lock:
            while True:
                path = self._chunk_path(self._next_seq)
                if path is None:
                    break
                if self._proc is None:
                    if path.suffix not in STREAMABLE_EXT:
                        self.disabled_reason = f"{path.suffix} форматыг шууд задлах боломжгүй"
                        logger.info("meeting %s: live transcription disabled (%s)", self.meeting_id, self.disabled_reason)
                        return
                    self._spawn_ffmpeg()
                data = await asyncio.to_thread(path.read_bytes)
                try:
                    await asyncio.to_thread(self._write, data)
                except (BrokenPipeError, OSError, ValueError):
                    self.disabled_reason = "ffmpeg зогссон"
                    logger.warning("meeting %s: live ffmpeg pipe closed", self.meeting_id)
                    return
                self._next_seq += 1
                self._last_activity = time.monotonic()
        self._wake.set()

    def request_flush(self) -> None:
        """Түр зогсоох үед: хуримтлагдсан бүх аудиог (урт хүрээгүй ч) текст болгоно."""
        self._flush_requested = True
        self._wake.set()

    def _chunk_path(self, seq: int) -> Optional[Path]:
        if not self.meeting_dir.is_dir():
            return None
        matches = list(self.meeting_dir.glob(f"{seq:06d}.*"))
        return matches[0] if matches else None

    def _spawn_ffmpeg(self) -> None:
        cmd = [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
        ]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._reader = threading.Thread(target=self._read_pcm, name=f"live-pcm:{self.meeting_id}", daemon=True)
        self._reader.start()

    def _write(self, data: bytes) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _read_pcm(self) -> None:
        assert self._proc and self._proc.stdout
        out = self._proc.stdout
        while True:
            data = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
            if not data:
                break
            if self._skip_bytes:
                drop = min(self._skip_bytes, len(data))
                self._skip_bytes -= drop
                data = data[drop:]
                if not data:
                    continue
            with self._lock:
                start_full = len(self._pcm) // FRAME_BYTES
                self._pcm.extend(data)
                end_full = len(self._pcm) // FRAME_BYTES
                for i in range(start_full, end_full):
                    self._frame_rms.append(_rms(bytes(self._pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])))

    # ---------- ASR цонх ----------

    def _take_window(self, force: bool) -> Optional[tuple[int, bytes]]:
        min_f = max(1, settings.live_min_sec * 1000 // FRAME_MS)
        target_f = max(min_f, settings.live_window_sec * 1000 // FRAME_MS)
        max_f = max(target_f, settings.live_max_sec * 1000 // FRAME_MS)
        with self._lock:
            n = len(self._frame_rms)
            cut = choose_cut(self._frame_rms, min_f, target_f, max_f)
            if cut is None and force and n >= 10:  # дор хаяж 1 сек
                cut = n
            if cut is None:
                return None
            pcm = bytes(self._pcm[: cut * FRAME_BYTES])
            del self._pcm[: cut * FRAME_BYTES]
            del self._frame_rms[:cut]
            start_sample = self._consumed_samples
            self._consumed_samples += cut * FRAME_SAMPLES
        return start_sample, pcm

    async def _worker(self) -> None:
        provider = asr_mod.asr_provider
        try:
            vocab = await asyncio.to_thread(vocabulary.for_meeting, self.meeting_id)
        except Exception:  # noqa: BLE001 — толь бичиггүй ч шууд бичвэр ажиллана
            logger.exception("meeting %s: vocabulary load failed", self.meeting_id)
            vocab = vocabulary.MeetingVocabulary()
        self._asr_context = asr_mod.ASRContext(hints=vocab.hints, topic=vocab.topic)
        self._corrections = vocab.corrections
        try:
            while not self._closed:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

                force = self._flush_requested
                self._flush_requested = False
                while not self._closed:
                    window = self._take_window(force)
                    if window is None:
                        break
                    await self._transcribe_window(provider, *window)

                if not self.listeners and time.monotonic() - self._last_activity > IDLE_TIMEOUT_SEC:
                    logger.info("meeting %s: live transcriber idle, closing", self.meeting_id)
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("meeting %s: live transcriber crashed", self.meeting_id)
        finally:
            self._shutdown_process()
            if _registry.get(self.meeting_id) is self:
                del _registry[self.meeting_id]

    def _save_window(self, start_ms: int, end_ms: int, text: str) -> None:
        try:
            with (self.meeting_dir / WINDOWS_FILE).open("a", encoding="utf-8") as f:
                f.write(json.dumps({"start_ms": start_ms, "end_ms": end_ms, "text": text}, ensure_ascii=False) + "\n")
        except OSError:  # хадгалж чадаагүй бол хурлын дараа энэ хэсгийг дахин текст болгоно
            logger.warning("meeting %s: could not save live window", self.meeting_id)

    async def _transcribe_window(self, provider: object, start_sample: int, pcm: bytes) -> None:
        start_ms = start_sample * 1000 // SAMPLE_RATE
        end_ms = start_ms + (len(pcm) // 2) * 1000 // SAMPLE_RATE
        if _rms(pcm) < MIN_SPEECH_RMS:
            await asyncio.to_thread(self._save_window, start_ms, end_ms, "")  # чимээгүй: хамрагдсан гэж үзнэ
            return
        try:
            raw = await provider.transcribe_wav_bytes(pcm_to_wav(pcm), context=self._asr_context)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 — шууд бичвэрийн алдаа бичлэгийг зогсоохгүй
            # Хадгалахгүй: хурлын дараа энэ завсрыг дахин текст болгоно
            logger.warning("meeting %s: live ASR failed: %s", self.meeting_id, e)
            await self._broadcast({"type": "transcript.error", "message": "Шууд бичвэр түр гарахгүй байна"})
            return
        raw = (raw or "").strip()
        await asyncio.to_thread(self._save_window, start_ms, end_ms, raw)
        text = vocabulary.apply_to_text(raw, self._corrections) if self._corrections and raw else raw
        if not text:
            return
        seg = {
            "type": "transcript.segment",
            "segment_id": f"live_{len(self.segments) + 1}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "status": "provisional",
        }
        self.segments.append(seg)
        await self._broadcast(seg)

    async def _broadcast(self, msg: dict) -> None:
        for listener in list(self.listeners):
            try:
                await listener(msg)
            except Exception:  # noqa: BLE001 — салсан холболт
                self.listeners.discard(listener)

    # ---------- хаах ----------

    def _shutdown_process(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        task = self._task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._shutdown_process()
        if _registry.get(self.meeting_id) is self:
            del _registry[self.meeting_id]


_registry: Dict[str, LiveTranscriber] = {}


def enabled() -> bool:
    return settings.live_transcription and asr_mod.supports_live(asr_mod.asr_provider)


def get_or_create(meeting_id: str, meeting_dir: Path) -> Optional[LiveTranscriber]:
    if not enabled():
        return None
    lt = _registry.get(meeting_id)
    if lt is None:
        lt = LiveTranscriber(meeting_id, meeting_dir)
        _registry[meeting_id] = lt
        lt.start()
    return lt


def get(meeting_id: str) -> Optional[LiveTranscriber]:
    return _registry.get(meeting_id)


async def stop(meeting_id: str) -> None:
    lt = _registry.get(meeting_id)
    if lt is not None:
        await lt.close()


async def stop_all() -> None:
    for lt in list(_registry.values()):
        await lt.close()
