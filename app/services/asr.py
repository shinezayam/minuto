from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Callable, List, Optional, Protocol

import httpx

from app.config import settings
from app.models.minutes import TranscriptSegment
from app.services.audio_utils import split_to_wav, wav_duration_ms

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Гадны AI үйлчилгээ (ASR/LLM) алдаа буцаасан."""


# Явцын мэдээлэл: (дууссан хэсэг, нийт хэсэг)
ProgressCallback = Optional[Callable[[int, int], None]]


class ASRProvider(Protocol):
    name: str

    async def transcribe(self, audio_path: str, on_progress: ProgressCallback = None) -> List[TranscriptSegment]: ...


class MockASRProvider:
    """Демо горим — бодит таних үйлдэл хийхгүй, тодорхой шошготой бичвэр буцаана."""

    name = "demo"

    async def transcribe(self, audio_path: str, on_progress: ProgressCallback = None) -> List[TranscriptSegment]:
        if on_progress:
            on_progress(1, 1)
        return [
            TranscriptSegment(
                id="seg_1",
                start_ms=0,
                end_ms=5000,
                text="[ДЕМО] Бодит яриа таних систем тохируулаагүй байна. ASR_PROVIDER=omni болгоно уу.",
            ),
        ]


OMNI_PROMPT = (
    "Энэ бол монгол хэл дээрх хурлын яриа. Ярьсан үгийг яг ярьсан ёсоор нь кирилл үсгээр бичнэ үү. "
    "Орчуулах, товчлох, засах, тайлбар нэмэхгүй. Сонсогдохгүй хэсгийг [...] гэж тэмдэглэ. "
    "Яриа огт байхгүй бол хоосон хариу буцаа. Зөвхөн бичвэрийг буцаа."
)


class QwenOmniASRProvider:
    """Qwen-Omni (DashScope OpenAI-compatible API)-аар аудиог текст болгоно."""

    name = "omni"

    def __init__(self) -> None:
        if not settings.qwen_api_key:
            raise RuntimeError("ASR_PROVIDER=omni боловч QWEN_API_KEY тохируулаагүй байна")

    async def transcribe(self, audio_path: str, on_progress: ProgressCallback = None) -> List[TranscriptSegment]:
        src = Path(audio_path)
        parts = await split_to_wav(src, src.parent / "_asr_parts", settings.omni_chunk_sec)
        if not parts:
            raise ValueError("Аудио хоосон байна")

        sem = asyncio.Semaphore(max(1, settings.asr_concurrency))
        chunk_ms = settings.omni_chunk_sec * 1000
        done = 0
        if on_progress:
            on_progress(0, len(parts))

        async with httpx.AsyncClient(timeout=settings.qwen_timeout_sec) as client:

            async def one(i: int, part: Path) -> TranscriptSegment:
                nonlocal done
                async with sem:
                    text = await self._transcribe_chunk(client, part)
                done += 1
                if on_progress:
                    on_progress(done, len(parts))
                start = i * chunk_ms
                return TranscriptSegment(
                    id=f"seg_{i + 1}",
                    start_ms=start,
                    end_ms=start + wav_duration_ms(part),
                    text=text,
                )

            segments = await asyncio.gather(*(one(i, p) for i, p in enumerate(parts)))

        return [s for s in segments if s.text]

    async def _transcribe_chunk(self, client: httpx.AsyncClient, wav: Path) -> str:
        b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
        body = {
            "model": settings.omni_model,
            "modalities": ["text"],
            "stream": True,  # Omni загварууд зөвхөн stream горимтой
            "temperature": 0,
            "enable_thinking": settings.qwen_enable_thinking,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": f"data:;base64,{b64}", "format": "wav"}},
                        {"type": "text", "text": OMNI_PROMPT},
                    ],
                }
            ],
        }

        last_error: Exception = ProviderError("Qwen-Omni: тодорхойгүй алдаа")
        for attempt in range(3):
            try:
                return await self._stream(client, body)
            except _ClientError as e:
                raise ProviderError(str(e)) from e  # буруу хүсэлт, key — дахин оролдох утгагүй
            except ProviderError as e:
                last_error = e
            except httpx.HTTPError as e:
                last_error = ProviderError(f"Qwen-Omni холболтын алдаа: {e}")
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise last_error

    async def _stream(self, client: httpx.AsyncClient, body: dict) -> str:
        url = settings.qwen_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {settings.qwen_api_key}"}
        text = []
        async with client.stream("POST", url, json=body, headers=headers) as res:
            if res.status_code != 200:
                detail = (await res.aread()).decode("utf-8", "replace")[:300]
                msg = f"Qwen-Omni HTTP {res.status_code}: {detail}"
                if 400 <= res.status_code < 500 and res.status_code != 429:
                    raise _ClientError(msg)
                raise ProviderError(msg)
            async for line in res.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise ProviderError(f"Qwen-Omni: {chunk['error']}")
                for choice in chunk.get("choices") or []:
                    text.append((choice.get("delta") or {}).get("content") or "")
        return "".join(text).strip()


class _ClientError(Exception):
    pass


class WhisperASRProvider:
    """
    Локал OpenAI Whisper (zzzchinguun-ийн хувилбар). Сервер дээр ажилладаг тул гадны API хэрэггүй,
    гэхдээ `medium` загварт ~2-5GB RAM хэрэгтэй, CPU дээр удаан. `pip install openai-whisper`.
    Монгол хэлэнд fine-tune хийсэн загварыг WHISPER_MODEL-оор зааж болно.
    """

    name = "whisper"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.model = None

    def _load_model(self):
        if self.model is None:
            import whisper

            logger.info("Loading Whisper model: %s", self.model_name)
            self.model = whisper.load_model(self.model_name)
            logger.info("Whisper model loaded on %s", self.model.device)
        return self.model

    def _transcribe_sync(self, audio_path: str) -> List[TranscriptSegment]:
        result = self._load_model().transcribe(audio_path, task="transcribe", verbose=False, fp16=False)
        logger.info("Whisper detected language: %s", result.get("language", "unknown"))
        segments = []
        for i, seg in enumerate(result["segments"]):
            text = seg["text"].strip()
            if text:
                segments.append(
                    TranscriptSegment(
                        id=f"seg_{i + 1}",
                        start_ms=int(seg["start"] * 1000),
                        end_ms=int(seg["end"] * 1000),
                        text=text,
                    )
                )
        return segments

    async def transcribe(self, audio_path: str, on_progress: ProgressCallback = None) -> List[TranscriptSegment]:
        try:
            if on_progress:
                on_progress(0, 1)
            # Whisper нь CPU-г удаан эзэлдэг тул event loop-ийг блоклохгүйн тулд thread-д ажиллуулна
            segments = await asyncio.to_thread(self._transcribe_sync, audio_path)
            if on_progress:
                on_progress(1, 1)
            return segments
        except Exception as e:
            # Хуурамч бичвэр рүү чимээгүй шилжихгүй — алдааг хэрэглэгчид харуулна
            raise ProviderError(f"Whisper: {e}") from e


def make_asr_provider() -> ASRProvider:
    if settings.asr_provider == "omni":
        return QwenOmniASRProvider()
    if settings.asr_provider == "whisper":
        return WhisperASRProvider(settings.whisper_model)
    if settings.asr_provider != "demo":
        raise RuntimeError(f"Тодорхойгүй ASR_PROVIDER: {settings.asr_provider}")
    return MockASRProvider()


asr_provider: ASRProvider = make_asr_provider()
