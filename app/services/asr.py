from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import shutil
import wave
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol

import httpx

from app.config import settings
from app.models.minutes import TranscriptSegment
from app.services import ai_usage
from app.services.audio_utils import extract_wav, to_flac, wav_bytes_duration_ms

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Гадны AI үйлчилгээ (ASR/LLM) алдаа буцаасан."""


# Явцын мэдээлэл: (дууссан хэсэг, нийт хэсэг)
ProgressCallback = Optional[Callable[[int, int], None]]


@dataclass(frozen=True)
class ASRContext:
    """ASR-д урьдчилан өгөх мэдээлэл: оролцогчдын нэр, хэрэглэгчийн толь бичгийн үгс, хурлын сэдэв."""

    hints: List[str] = field(default_factory=list)
    topic: Optional[str] = None
    # Үргэлжлүүлэх: өмнө дууссан хэсгүүд {index: {"text","start_ms","end_ms"}} — дахин хийхгүй
    resume: dict = field(default_factory=dict)
    # Хэсэг бүр дуусах бүрд дуудагдана (durable job DB-д хадгална)
    on_chunk: Optional[Callable[[int, dict], None]] = None
    # Хурлын үеэр аль хэдийн текст болгосон цонхнууд [{"start_ms","end_ms","text"}]. Эдгээрийг дахин ASR-д
    # явуулахгүй, зөвхөн хамрагдаагүй завсрыг (алдаа гарсан цонх, сүүлийн хэсэг) текст болгоно.
    prefilled: List[dict] = field(default_factory=list)


NO_CONTEXT = ASRContext()


# Завсар үүнээс богино бол ASR-д явуулахгүй (чимээгүй дээр Omni зохиож бичихээс сэргийлнэ, зардал хэмнэнэ)
MIN_GAP_MS = 1000
# Завсар нөхөх хэсгүүдийн resume түлхүүр: энгийн 30 сек-ийн хэсгүүдийн дугаартай давхцахгүй
GAP_KEY_BASE = 1_000_000


def plan_gaps(total_ms: int, covered: List[dict], chunk_ms: int, min_gap_ms: int = MIN_GAP_MS) -> List[tuple[int, int]]:
    """[0, total_ms] дотроос covered цонхнуудад ороогүй завсруудыг chunk_ms-ээс урт биш хэсгүүдээр буцаана."""
    gaps: List[tuple[int, int]] = []
    pos = 0
    for w in sorted(covered, key=lambda w: w["start_ms"]):
        start, end = max(0, int(w["start_ms"])), min(total_ms, int(w["end_ms"]))
        if start - pos >= min_gap_ms:
            gaps.append((pos, start))
        pos = max(pos, end)
    if total_ms - pos >= min_gap_ms:
        gaps.append((pos, total_ms))
    pieces = []
    for a, b in gaps:
        n = math.ceil((b - a) / chunk_ms)
        step = math.ceil((b - a) / n)
        pieces += [(x, min(b, x + step)) for x in range(a, b, step)]
    return pieces


class ASRProvider(Protocol):
    name: str

    async def transcribe(
        self, audio_path: str, on_progress: ProgressCallback = None, context: ASRContext = NO_CONTEXT
    ) -> List[TranscriptSegment]: ...


class LiveASRProvider(Protocol):
    """Хурлын үеэр богино (10-25 сек) wav хэсгийг шууд текст болгож чаддаг provider."""

    async def transcribe_wav_bytes(self, wav: bytes, context: ASRContext = NO_CONTEXT) -> str: ...


def supports_live(provider: object) -> bool:
    return callable(getattr(provider, "transcribe_wav_bytes", None))


class MockASRProvider:
    """Демо горим — бодит таних үйлдэл хийхгүй, тодорхой шошготой бичвэр буцаана."""

    name = "demo"

    async def transcribe(
        self, audio_path: str, on_progress: ProgressCallback = None, context: ASRContext = NO_CONTEXT
    ) -> List[TranscriptSegment]:
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

    async def transcribe_wav_bytes(self, wav: bytes, context: ASRContext = NO_CONTEXT) -> str:
        return "[ДЕМО] шууд бичвэр"


OMNI_PROMPT = (
    "Энэ бол монгол хэл дээрх хурлын яриа. Ярьсан үгийг яг ярьсан ёсоор нь кирилл үсгээр бичнэ үү. "
    "Орчуулах, товчлох, засах, тайлбар нэмэхгүй. Сонсогдохгүй хэсгийг [...] гэж тэмдэглэ. "
    "Яригчийг дуу хоолойгоор нь ялгаж, хүн бүрийн яриаг шинэ мөрөөс \"Яригч 1: \", \"Яригч 2: \" гэж эхлүүл "
    "(анх ярьсан дарааллаар дугаарла; ганц хүн ярьж байвал бүх мөр \"Яригч 1: \"). "
    "Асуултын төгсгөлд асуултын тэмдэг (?) тавь. "
    "Яриа огт байхгүй бол хоосон хариу буцаа. Зөвхөн бичвэрийг буцаа."
)


def build_omni_prompt(context: ASRContext) -> str:
    """Үндсэн заавар + (байвал) хурлын сэдэв ба гарч болох нэр, үгс."""
    extra = []
    if context.topic:
        extra.append(f"Хурлын сэдэв: {context.topic}.")
    if context.hints:
        extra.append(
            "Энэ хуралд гарч болох нэр, үгс: "
            + ", ".join(context.hints)
            + ". Эдгээр үг сонсогдвол яг энэ бичлэгээр бич. Сонсогдоогүй үгийг бүү нэм."
        )
    return OMNI_PROMPT if not extra else OMNI_PROMPT + " " + " ".join(extra)


class QwenOmniASRProvider:
    """Qwen-Omni (DashScope OpenAI-compatible API)-аар аудиог текст болгоно."""

    name = "omni"

    def __init__(self) -> None:
        if not settings.qwen_api_key:
            raise RuntimeError("ASR_PROVIDER=omni боловч QWEN_API_KEY тохируулаагүй байна")

    async def transcribe(
        self, audio_path: str, on_progress: ProgressCallback = None, context: ASRContext = NO_CONTEXT
    ) -> List[TranscriptSegment]:
        """Дискийг бага ашиглана: аудиог НЭГ удаа mono FLAC болгоод (WAV-аас ~3 дахин бага), 30 секундын хэсэг бүрийг
        хэрэгтэй үед нь гаргаж, илгээмэгц устгана. Оргил диск ≈ FLAC + (зэрэг ажиллах тоо × ~1MB). Дуусахад бүгд устна.
        (Өмнө нь бүх хэсгийг урьдчилан WAV болгож — минутад ~1.9MB — хэзээ ч устгадаггүй байсан.)"""
        src = Path(audio_path)
        prompt = build_omni_prompt(context)
        work = src.parent / "_asr_work"
        shutil.rmtree(src.parent / "_asr_parts", ignore_errors=True)  # хуучин хувилбарын үлдсэн хэсгүүд
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            flac = work / "source.flac"
            total_sec = await to_flac(src, flac)
            chunk_ms = settings.omni_chunk_sec * 1000
            total_ms = int(round(total_sec, 3) * 1000)
            if context.prefilled:
                # Шууд бичлэг: хурлын үеэр гарсан бичвэрийг ашиглаж, зөвхөн дутуу хэсгийг дахин текст болгоно
                pieces = plan_gaps(total_ms, context.prefilled, chunk_ms)
                key_base = GAP_KEY_BASE
                reused = [
                    TranscriptSegment(id="", start_ms=int(w["start_ms"]), end_ms=min(total_ms, int(w["end_ms"])), text=w["text"])
                    for w in context.prefilled if (w.get("text") or "").strip() and int(w["start_ms"]) < total_ms
                ]
            else:
                n_grid = max(0, math.ceil(total_ms / chunk_ms))
                if n_grid == 0:
                    raise ValueError("Аудио хоосон байна")
                pieces = [(i * chunk_ms, min(total_ms, (i + 1) * chunk_ms)) for i in range(n_grid)]
                key_base = 0
                reused = []
            n_parts = len(pieces)

            sem = asyncio.Semaphore(max(1, settings.asr_concurrency))
            done = 0
            if on_progress:
                on_progress(0, n_parts)

            async with httpx.AsyncClient(timeout=settings.qwen_timeout_sec) as client:

                async def one(i: int) -> TranscriptSegment:
                    nonlocal done
                    start, stop = pieces[i]
                    key = key_base + i
                    saved = context.resume.get(key)
                    if saved is not None:  # өмнөх оролдлогод дууссан: API дуудахгүй, хэсэг гаргахгүй
                        text, end = saved["text"], saved["end_ms"]
                    else:
                        async with sem:
                            # Энгийн горимд хэсгийн урт тогтмол (сүүлийнх нь файлын төгсгөл хүртэл)
                            dur = (stop - start) / 1000 if key_base else chunk_ms / 1000
                            wav = await extract_wav(flac, start / 1000, dur, work / f"part_{key:07d}.wav")
                            end = start + wav_bytes_duration_ms(wav)
                            text = await self._transcribe_bytes(client, wav, prompt)
                        if context.on_chunk:
                            context.on_chunk(key, {"text": text, "start_ms": start, "end_ms": end})
                    done += 1
                    if on_progress:
                        on_progress(done, n_parts)
                    return TranscriptSegment(id="", start_ms=start, end_ms=end, text=text)

                segments = list(await asyncio.gather(*(one(i) for i in range(n_parts))))
        finally:
            shutil.rmtree(work, ignore_errors=True)

        segments = sorted([s for s in reused + segments if s.text], key=lambda s: s.start_ms)
        for i, seg in enumerate(segments):
            seg.id = f"seg_{i + 1}"
        return segments

    async def transcribe_wav_bytes(self, wav: bytes, context: ASRContext = NO_CONTEXT) -> str:
        async with httpx.AsyncClient(timeout=settings.qwen_timeout_sec) as client:
            return await self._transcribe_bytes(client, wav, build_omni_prompt(context))

    async def _transcribe_bytes(self, client: httpx.AsyncClient, wav: bytes, prompt: str = OMNI_PROMPT) -> str:
        b64 = base64.b64encode(wav).decode("ascii")
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
                        {"type": "text", "text": prompt},
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


SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_SENTENCE_END = (".", "?", "!", "…")


def scribe_keyterms(hints: List[str]) -> List[str]:
    """Scribe-ийн keyterms: тусгай тэмдэгтгүй, ≤50 тэмдэгт, ≤5 үг, ≤1000 ширхэг."""
    terms = []
    for h in hints:
        t = "".join(ch for ch in h if ch not in "<>{}[]\\")[:50].strip()
        if t and len(t.split()) <= 5 and t not in terms:
            terms.append(t)
    return terms[:1000]


def scribe_words_to_segments(words: List[dict], max_ms: int) -> List[TranscriptSegment]:
    """Scribe-ийн үгсийг ≤max_ms сегментүүд болгоно. Яригч солигдох эсвэл өгүүлбэр дуусах цэг дээр таслахыг
    илүүд үзнэ; хэт урт ганц хүний яриаг (1.5×max_ms) үгийн заагаар таслана. Шошго ("Яригч N") нь бүтэн хурлын
    хэмжээнд нэг утгатай (анх ярьсан дарааллаар)."""
    labels: dict = {}
    segments: List[TranscriptSegment] = []
    seg_start: Optional[int] = None
    seg_end = 0
    turns: List[list] = []  # [[label, [tokens]]]

    def close() -> None:
        nonlocal seg_start, turns
        lines = [f"{label}: {''.join(tokens).strip()}" for label, tokens in turns if "".join(tokens).strip()]
        if lines and seg_start is not None:
            segments.append(TranscriptSegment(id=f"seg_{len(segments) + 1}", start_ms=seg_start,
                                              end_ms=max(seg_end, seg_start), text="\n".join(lines)))
        seg_start, turns = None, []

    for w in words:
        kind = w.get("type") or "word"
        if kind == "spacing":
            if turns:
                turns[-1][1].append(w.get("text") or " ")
            continue
        if kind != "word" or not (w.get("text") or "").strip():
            continue
        label = labels.setdefault(w.get("speaker_id") or "speaker_0", f"Яригч {len(labels) + 1}")
        start = int(round(float(w.get("start") or 0) * 1000))
        end = int(round(float(w.get("end") or 0) * 1000))
        if seg_start is not None and end - seg_start > max_ms:
            last = "".join(turns[-1][1]).rstrip() if turns else ""
            if label != turns[-1][0] or last.endswith(_SENTENCE_END) or end - seg_start > max_ms * 1.5:
                close()
        if seg_start is None:
            seg_start = start
        if turns and turns[-1][0] == label:
            turns[-1][1].append(w["text"])
        else:
            turns.append([label, [w["text"]]])
        seg_end = max(seg_end, end)
    close()
    return segments


class ScribeASRProvider:
    """ElevenLabs Scribe: бүтэн файлыг нэг дуудлагаар илгээнэ, яригчийг бүтэн хурлын хэмжээнд ялгана (diarize).
    Хурлын үеийн шууд бичвэрийг (prefilled) дахин ашиглахгүй: тэр нь зөвхөн дэлгэцэнд, эцсийн бичвэр бүтэн файлаас."""

    name = "scribe"

    def __init__(self) -> None:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ASR_PROVIDER=scribe боловч ELEVENLABS_API_KEY тохируулаагүй байна")

    async def transcribe(
        self, audio_path: str, on_progress: ProgressCallback = None, context: ASRContext = NO_CONTEXT
    ) -> List[TranscriptSegment]:
        saved = context.resume.get(0)
        if saved and isinstance(saved.get("segments"), list):  # өмнөх оролдлогод дууссан: дахин төлөхгүй
            return [TranscriptSegment(**s) for s in saved["segments"]]
        if on_progress:
            on_progress(0, 1)
        data: dict = {
            "model_id": settings.scribe_model,
            "language_code": "mn",
            "diarize": "true",
            "tag_audio_events": "false",
            "timestamps_granularity": "word",
        }
        terms = scribe_keyterms(context.hints)
        if terms:
            data["keyterms"] = terms
        body = await self._post(Path(audio_path), data)
        ends = [w.get("end") for w in body.get("words") or [] if isinstance(w, dict) and isinstance(w.get("end"), (int, float))]
        await ai_usage.record_asr(max(ends, default=0.0), live=False, keyterms=bool(terms), provider="scribe",
                                  model=settings.scribe_model)
        segments = scribe_words_to_segments(body.get("words") or [], settings.scribe_segment_sec * 1000)
        if not segments and (body.get("text") or "").strip():  # үгийн мэдээлэлгүй хариу (ховор)
            segments = [TranscriptSegment(id="seg_1", start_ms=0, end_ms=0, text=f"Яригч 1: {body['text'].strip()}")]
        if context.on_chunk:
            context.on_chunk(0, {"segments": [s.model_dump() for s in segments]})
        if on_progress:
            on_progress(1, 1)
        return segments

    async def transcribe_wav_bytes(self, wav: bytes, context: ASRContext = NO_CONTEXT) -> str:
        """Хурлын үеийн богино цонх (10-25с): яригч ялгахгүй, зөвхөн текст."""
        data: dict = {"model_id": settings.scribe_model, "language_code": "mn", "diarize": "false",
                      "tag_audio_events": "false"}
        terms = scribe_keyterms(context.hints)
        if terms:
            data["keyterms"] = terms
        body = await self._post_bytes(wav, "window.wav", data, timeout=60)
        try:
            seconds = wav_bytes_duration_ms(wav) / 1000
        except (wave.Error, EOFError, ZeroDivisionError):  # уншигдахгүй толгой: зардлыг 0 гэж бүртгэнэ
            seconds = 0.0
        await ai_usage.record_asr(seconds, live=True, keyterms=bool(terms), provider="scribe", model=settings.scribe_model)
        return (body.get("text") or "").strip()

    async def _post(self, path: Path, data: dict) -> dict:
        with path.open("rb") as fh:
            return await self._send(data, {"file": (path.name, fh, "application/octet-stream")}, settings.scribe_timeout_sec)

    async def _post_bytes(self, blob: bytes, name: str, data: dict, timeout: int) -> dict:
        return await self._send(data, {"file": (name, blob, "audio/wav")}, timeout)

    async def _send(self, data: dict, files: dict, timeout: int) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(SCRIBE_URL, headers={"xi-api-key": settings.elevenlabs_api_key},
                                        data=data, files=files)
        except httpx.HTTPError as e:
            raise ProviderError(f"Scribe холболтын алдаа: {type(e).__name__}: {e}") from e
        if res.status_code != 200:
            raise ProviderError(f"Scribe HTTP {res.status_code}: {res.text[:300]}")
        try:
            return res.json()
        except ValueError as e:
            raise ProviderError(f"Scribe хариу JSON биш: {res.text[:200]}") from e


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

    def _transcribe_sync(self, audio_path: str, context: ASRContext = NO_CONTEXT) -> List[TranscriptSegment]:
        kwargs = {}
        if context.hints or context.topic:
            # Whisper-ийн initial_prompt нь үгсийн бичлэгийг чиглүүлдэг (заавар биш, жишээ текст)
            kwargs["initial_prompt"] = ", ".join([*([context.topic] if context.topic else []), *context.hints])
        result = self._load_model().transcribe(audio_path, task="transcribe", verbose=False, fp16=False, **kwargs)
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

    async def transcribe(
        self, audio_path: str, on_progress: ProgressCallback = None, context: ASRContext = NO_CONTEXT
    ) -> List[TranscriptSegment]:
        try:
            if on_progress:
                on_progress(0, 1)
            # Whisper нь CPU-г удаан эзэлдэг тул event loop-ийг блоклохгүйн тулд thread-д ажиллуулна
            segments = await asyncio.to_thread(self._transcribe_sync, audio_path, context)
            if on_progress:
                on_progress(1, 1)
            return segments
        except Exception as e:
            # Хуурамч бичвэр рүү чимээгүй шилжихгүй — алдааг хэрэглэгчид харуулна
            raise ProviderError(f"Whisper: {e}") from e


def make_asr_provider() -> ASRProvider:
    if settings.asr_provider == "scribe":
        return ScribeASRProvider()
    if settings.asr_provider == "omni":
        return QwenOmniASRProvider()
    if settings.asr_provider == "whisper":
        return WhisperASRProvider(settings.whisper_model)
    if settings.asr_provider != "demo":
        raise RuntimeError(f"Тодорхойгүй ASR_PROVIDER: {settings.asr_provider}")
    return MockASRProvider()


asr_provider: ASRProvider = make_asr_provider()
