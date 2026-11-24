"""ASR / LLM provider-ууд — гадны API-г httpx.MockTransport-оор орлуулж шалгана."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.models.minutes import TranscriptSegment
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.services.audio_utils import ffmpeg_exe, find_meeting_audio, split_to_wav


def _ffmpeg_available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except RuntimeError:
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg алга")


def _make_tone(path: Path, seconds: int) -> None:
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", str(path)],
        check=True,
    )


@pytest.fixture
def qwen_key(monkeypatch):
    monkeypatch.setattr(settings, "qwen_api_key", "test-key")


def _patch_httpx(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _sse(*texts: str) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


# ---------- audio ----------

@needs_ffmpeg
async def test_split_to_wav(tmp_path):
    src = tmp_path / "a.mp3"
    _make_tone(src, 25)
    parts = await split_to_wav(src, tmp_path / "parts", chunk_sec=10)
    assert len(parts) == 3


def test_find_meeting_audio_prefers_upload(tmp_path):
    (tmp_path / "000000.webm").write_bytes(b"a")
    (tmp_path / "uploaded.mp3").write_bytes(b"x")
    assert find_meeting_audio(tmp_path).name == "uploaded.mp3"


def test_find_meeting_audio_concatenates_chunks_in_order(tmp_path):
    (tmp_path / "000001.bin").write_bytes(b"B")
    (tmp_path / "000000.webm").write_bytes(b"A")
    (tmp_path / "000002.bin").write_bytes(b"C")
    out = find_meeting_audio(tmp_path)
    assert out.name == "recorded.webm"
    assert out.read_bytes() == b"ABC"


def test_find_meeting_audio_none(tmp_path):
    assert find_meeting_audio(tmp_path / "missing") is None
    assert find_meeting_audio(tmp_path) is None


# ---------- Qwen-Omni ASR ----------

@needs_ffmpeg
async def test_omni_transcribes_each_chunk(tmp_path, qwen_key, monkeypatch):
    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    src = tmp_path / "meeting.mp3"
    _make_tone(src, 25)

    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["authorization"] == "Bearer test-key"
        assert body["stream"] is True
        audio = body["messages"][0]["content"][0]["input_audio"]
        assert audio["format"] == "wav" and audio["data"].startswith("data:;base64,")
        n = len(calls)
        return httpx.Response(200, content=_sse("Сайн ", f"байна уу {n}"))

    _patch_httpx(monkeypatch, handler)
    segs = await asr_mod.QwenOmniASRProvider().transcribe(str(src))

    assert len(calls) == 3
    assert [s.id for s in segs] == ["seg_1", "seg_2", "seg_3"]
    assert segs[0].start_ms == 0 and segs[1].start_ms == 10_000
    assert 24_000 <= segs[2].end_ms <= 26_000
    assert all(s.text.startswith("Сайн байна уу") for s in segs)


def test_plan_gaps_only_uncovered_ranges():
    covered = [{"start_ms": 0, "end_ms": 12_000}, {"start_ms": 12_000, "end_ms": 20_000}, {"start_ms": 45_000, "end_ms": 50_000}]
    # 20–45с завсар (25с) 10с-ээс хэтрэхгүй хэсгүүдэд, төгсгөлийн 50–52с, 0.5с-ийн завсрыг алгасна
    assert asr_mod.plan_gaps(52_000, covered, 10_000) == [
        (20_000, 28_334), (28_334, 36_668), (36_668, 45_000), (50_000, 52_000),
    ]
    assert asr_mod.plan_gaps(50_500, covered, 10_000)[-1] == (36_668, 45_000)
    assert asr_mod.plan_gaps(30_000, [], 30_000) == [(0, 30_000)]


@needs_ffmpeg
async def test_omni_reuses_live_windows_and_fills_gaps_only(tmp_path, qwen_key, monkeypatch):
    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    src = tmp_path / "meeting.mp3"
    _make_tone(src, 25)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=_sse("нөхсөн"))

    _patch_httpx(monkeypatch, handler)
    live = [
        {"start_ms": 0, "end_ms": 9_000, "text": "Яригч 1: эхний"},
        {"start_ms": 9_000, "end_ms": 14_000, "text": ""},  # чимээгүй
        {"start_ms": 14_000, "end_ms": 19_000, "text": "Яригч 1: дунд"},
    ]
    saved = {}
    ctx = asr_mod.ASRContext(prefilled=live, on_chunk=lambda i, d: saved.__setitem__(i, d))
    segs = await asr_mod.QwenOmniASRProvider().transcribe(str(src), context=ctx)

    assert len(calls) == 1  # зөвхөн 19–25с
    assert [s.text for s in segs] == ["Яригч 1: эхний", "Яригч 1: дунд", "нөхсөн"]
    assert [s.id for s in segs] == ["seg_1", "seg_2", "seg_3"]
    assert segs[2].start_ms == 19_000 and 24_000 <= segs[2].end_ms <= 26_000
    assert list(saved) == [asr_mod.GAP_KEY_BASE]

    # Дахин оролдоход хадгалсан хэсгийг ч дахин дуудахгүй
    calls.clear()
    ctx2 = asr_mod.ASRContext(prefilled=live, resume=saved)
    again = await asr_mod.QwenOmniASRProvider().transcribe(str(src), context=ctx2)
    assert calls == [] and [s.text for s in again] == [s.text for s in segs]


@needs_ffmpeg
async def test_omni_auth_error_not_retried(tmp_path, qwen_key, monkeypatch):
    src = tmp_path / "m.mp3"
    _make_tone(src, 3)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "Invalid API-key"}})

    _patch_httpx(monkeypatch, handler)
    with pytest.raises(asr_mod.ProviderError, match="401"):
        await asr_mod.QwenOmniASRProvider().transcribe(str(src))
    assert len(calls) == 1


def test_omni_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "qwen_api_key", "")
    with pytest.raises(RuntimeError):
        asr_mod.QwenOmniASRProvider()


# ---------- Whisper (локал) ----------

def _fake_whisper(monkeypatch, transcribe):
    import sys
    import types

    model = types.SimpleNamespace(device="cpu", transcribe=transcribe)
    monkeypatch.setitem(sys.modules, "whisper", types.SimpleNamespace(load_model=lambda name: model))


async def test_whisper_segments(monkeypatch):
    _fake_whisper(monkeypatch, lambda path, **kw: {
        "language": "mn",
        "segments": [{"start": 0.0, "end": 2.5, "text": " Сайн байна уу "}, {"start": 2.5, "end": 3.0, "text": "  "}],
    })
    segs = await asr_mod.WhisperASRProvider("tiny").transcribe("x.wav")
    assert [(s.id, s.start_ms, s.end_ms, s.text) for s in segs] == [("seg_1", 0, 2500, "Сайн байна уу")]


async def test_whisper_failure_is_not_hidden(monkeypatch):
    def boom(path, **kw):
        raise MemoryError("out of memory")

    _fake_whisper(monkeypatch, boom)
    with pytest.raises(asr_mod.ProviderError, match="Whisper"):
        await asr_mod.WhisperASRProvider("medium").transcribe("x.wav")


# ---------- Qwen LLM ----------

TRANSCRIPT = [
    TranscriptSegment(id="seg_1", start_ms=0, end_ms=30_000, text="Төслийг ирэх сарын 1-нд хүлээлгэн өгөхөөр шийдлээ."),
    TranscriptSegment(id="seg_2", start_ms=30_000, end_ms=60_000, text="Бат тайланг баасан гаригт бэлдэнэ."),
]


def _chat(content: dict | str) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_qwen_minutes_valid(qwen_key, monkeypatch):
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["body"] = body
        return _chat({
            "title": "Төслийн хурал",
            "summary": "Хүлээлгэн өгөх хугацааг тогтов.",
            "topics": [],
            "decisions": [{"text": "1-нд хүлээлгэн өгнө", "source_segment_ids": ["seg_1", "seg_99"]}],
            "action_items": [{"task": "Тайлан бэлдэх", "owner": "Бат", "due_date": "Баасан", "source_segment_ids": ["seg_2"]}],
            "open_questions": [],
        })

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)

    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "[seg_2 00:30-01:00]" in seen["body"]["messages"][1]["content"]
    assert m.title == "Төслийн хурал"
    assert m.decisions[0].source_segment_ids == ["seg_1"]  # байхгүй seg_99 хасагдсан
    assert m.action_items[0].owner == "Бат"


async def test_qwen_minutes_repairs_invalid_json(qwen_key, monkeypatch):
    replies = iter(["not json", json.dumps({"title": "Х", "summary": "Т"})])
    _patch_httpx(monkeypatch, lambda req: _chat(next(replies)))
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)
    assert m.title == "Х"


async def test_qwen_minutes_tolerates_messy_json(qwen_key, monkeypatch):
    """Урт бодит хурал дээр загвар schema-аас бага зэрэг зөрдөг: энэ нь бүтэн хурлын бичгийг унагахгүй."""
    messy = {
        "title": "Хурал",
        "summary": ["Нэгдүгээр өгүүлбэр.", "Хоёрдугаар өгүүлбэр."],
        "topics": ["Төсөв", {"title": None}, {"title": "Хуваарь", "description": None, "source_segment_ids": "seg_1"}],
        "decisions": [{"text": "1-нд өгнө", "status": "Rejected", "confidence": "0.9", "evidence": [{"quote": "x"}],
                       "source_segment_ids": ["seg_1"]}],
        "action_items": [{"task": "Тайлан", "owner": ["Бат", "Дорж"], "due_date": {"text": "Баасан"}, "status": None,
                          "confidence": 7}],
        "open_questions": None,
        "participants": ["Бат", None, 3],
        "qa": [{"question": "Хэзээ?", "answer": None}],
        "speaker_map": {"seg_1": {"Яригч 1": "Бат", "Яригч 2": None}, "seg_2": None},
    }
    _patch_httpx(monkeypatch, lambda req: _chat("```json\n" + json.dumps(messy, ensure_ascii=False) + "\n```"))
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)
    assert m.summary == "Нэгдүгээр өгүүлбэр. Хоёрдугаар өгүүлбэр."
    assert [t.title for t in m.topics] == ["Төсөв", "Хуваарь"]
    assert m.topics[1].source_segment_ids == ["seg_1"]
    assert m.decisions[0].status == "rejected" and m.decisions[0].confidence == 0.9
    assert m.action_items[0].owner == "Бат, Дорж" and m.action_items[0].due_date == "Баасан"
    assert m.action_items[0].status == "open" and m.action_items[0].confidence == 1.0
    assert m.open_questions == [] and m.participants == ["Бат", "3"]
    assert m.qa[0].question == "Хэзээ?"


async def test_qwen_minutes_retry_does_not_resend_bad_reply(qwen_key, monkeypatch):
    sent = []
    replies = iter(["[1, 2]", json.dumps({"title": "Х", "summary": "Т"})])

    def handler(req):
        sent.append(json.loads(req.content)["messages"])
        return _chat(next(replies))

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)
    assert m.title == "Х"
    assert len(sent[1]) == 3 and all(msg["role"] != "assistant" for msg in sent[1])


async def test_qwen_minutes_gives_up(qwen_key, monkeypatch):
    _patch_httpx(monkeypatch, lambda req: _chat("still not json"))
    with pytest.raises(asr_mod.ProviderError):
        await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)


async def test_qwen_minutes_empty_transcript(qwen_key):
    with pytest.raises(ValueError):
        await llm_mod.QwenLLMProvider().generate_minutes([])


async def _upload_tone(client, tmp_path, seconds: int) -> str:
    audio = tmp_path / "in.mp3"
    _make_tone(audio, seconds)
    mid = (await client.post("/api/meetings", json={"title": "E2E"})).json()["id"]
    r = await client.post(f"/api/meetings/{mid}/upload", files={"file": ("in.mp3", audio.read_bytes(), "audio/mpeg")})
    assert r.status_code == 200, r.text
    return mid


@needs_ffmpeg
async def test_process_runs_in_background_end_to_end(client, tmp_path, qwen_key, monkeypatch):
    """upload → process (202, ард нь) → явц → transcript → minutes → DOCX"""
    from app.services import processing

    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    monkeypatch.setattr(asr_mod, "asr_provider", asr_mod.QwenOmniASRProvider())
    monkeypatch.setattr(llm_mod, "llm_provider", llm_mod.QwenLLMProvider())
    mid = await _upload_tone(client, tmp_path, 15)

    def handler(req):
        body = json.loads(req.content)
        if body.get("stream"):
            return httpx.Response(200, content=_sse("Бат тайлан бэлдэнэ."))
        return _chat({"title": "Тест", "summary": "Товч", "open_questions": [{"text": "Төсөв?"}]})

    _patch_httpx(monkeypatch, handler)  # app-ийн client-ийг үүсгэсний дараа patch хийнэ
    r = await client.post(f"/api/meetings/{mid}/process")
    assert r.status_code == 202 and r.json()["status"] == "processing"
    assert (await client.get(f"/api/meetings/{mid}")).json()["status"] == "processing"
    # Давхар эхлүүлэхийг хориглоно
    assert (await client.post(f"/api/meetings/{mid}/process")).status_code == 409

    await processing.wait_all()
    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["status"] == "completed", m
    assert (m["processing_stage"], m["processing_done"], m["processing_total"]) == ("done", 2, 2)
    assert m["processing_error"] is None

    segs = (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]
    assert segs[0]["text"] == "Бат тайлан бэлдэнэ." and len(segs) == 2
    minutes = (await client.get(f"/api/meetings/{mid}/minutes")).json()
    assert minutes["open_questions"][0]["text"] == "Төсөв?"
    docx = await client.get(f"/api/meetings/{mid}/export.docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"

    # Дахин боловсруулж болно
    assert (await client.post(f"/api/meetings/{mid}/process")).status_code == 202
    await processing.wait_all()


@needs_ffmpeg
async def test_summary_failure_keeps_transcript(client, tmp_path, qwen_key, monkeypatch):
    from app.services import processing

    monkeypatch.setattr(asr_mod, "asr_provider", asr_mod.QwenOmniASRProvider())
    monkeypatch.setattr(llm_mod, "llm_provider", llm_mod.QwenLLMProvider())
    mid = await _upload_tone(client, tmp_path, 3)

    def handler(req):
        if json.loads(req.content).get("stream"):
            return httpx.Response(200, content=_sse("Сайн байна уу"))
        return httpx.Response(500, text="boom")

    _patch_httpx(monkeypatch, handler)
    await client.post(f"/api/meetings/{mid}/process")
    await processing.wait_all()

    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["status"] == "failed"
    assert m["processing_error"].startswith("SUMMARY_FAILED")
    segs = (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]
    assert segs[0]["text"] == "Сайн байна уу"


@needs_ffmpeg
async def test_no_speech_marks_failed(client, tmp_path, qwen_key, monkeypatch):
    from app.services import processing

    monkeypatch.setattr(asr_mod, "asr_provider", asr_mod.QwenOmniASRProvider())
    mid = await _upload_tone(client, tmp_path, 3)
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, content=_sse("")))
    await client.post(f"/api/meetings/{mid}/process")
    await processing.wait_all()
    assert (await client.get(f"/api/meetings/{mid}")).json()["processing_error"].startswith("NO_SPEECH")


async def test_process_without_audio_returns_400(client):
    mid = (await client.post("/api/meetings", json={"title": "x"})).json()["id"]
    r = await client.post(f"/api/meetings/{mid}/process")
    assert r.status_code == 400


async def test_interrupted_processing_is_failed_on_restart(client):
    from app.services import meeting_store

    mid = (await client.post("/api/meetings", json={"title": "x"})).json()["id"]
    assert meeting_store.try_begin_processing(mid)
    assert meeting_store.fail_interrupted_processing() == 1
    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["status"] == "failed" and m["processing_error"].startswith("INTERRUPTED")


async def test_qwen_http_error(qwen_key, monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(asr_mod.ProviderError, match="500"):
        await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)


# ---------- Дискийн ачаалал багатай ASR (FLAC нэг удаа, хэсгийг хэрэгтэй үед нь) ----------

@needs_ffmpeg
async def test_omni_leaves_no_temp_files_and_cleans_legacy_parts(tmp_path, qwen_key, monkeypatch):
    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    src = tmp_path / "meeting.mp3"
    _make_tone(src, 25)
    legacy = tmp_path / "_asr_parts"
    legacy.mkdir()
    (legacy / "part_0000.wav").write_bytes(b"x" * 1000)  # хуучин хувилбарын үлдэгдэл
    seen_work_files: list = []

    def handler(req):
        work = tmp_path / "_asr_work"
        seen_work_files.append(sorted(p.name for p in work.iterdir()) if work.exists() else [])
        return httpx.Response(200, content=_sse("ok"))

    _patch_httpx(monkeypatch, handler)
    segs = await asr_mod.QwenOmniASRProvider().transcribe(str(src))
    assert len(segs) == 3
    # Хэсгүүдийг урьдчилан бүгдийг нь биш, зэрэг ажиллах тооноос хэтрэхгүйгээр гаргаж, уншмагц устгана
    assert all("source.flac" in files for files in seen_work_files)
    assert max(len(files) - 1 for files in seen_work_files) <= settings.asr_concurrency
    assert not (tmp_path / "_asr_work").exists() and not legacy.exists()
    assert not [p.name for p in tmp_path.iterdir() if p.name.startswith("_asr")]


@needs_ffmpeg
async def test_omni_cleans_up_after_provider_failure(tmp_path, qwen_key, monkeypatch):
    src = tmp_path / "m.mp3"
    _make_tone(src, 3)
    _patch_httpx(monkeypatch, lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(asr_mod.ProviderError):
        await asr_mod.QwenOmniASRProvider().transcribe(str(src))
    assert not (tmp_path / "_asr_work").exists()


async def test_disk_full_is_retried_and_provider_text_is_hidden(monkeypatch):
    import errno

    from app.services import jobs, processing

    class Job:
        chunks: dict = {}
        def can_retry(self):
            return True
        def save_chunk(self, i, d):
            pass

    monkeypatch.setattr(processing, "find_meeting_audio", lambda d: Path("x.wav"))
    monkeypatch.setattr(processing.audio_store, "ensure_local", lambda mid: True)
    monkeypatch.setattr(processing.vocabulary, "for_meeting",
                        lambda mid: type("V", (), {"hints": [], "topic": None, "corrections": []})())
    progress: list = []
    finished: list = []
    monkeypatch.setattr(processing.meeting_store, "set_progress", lambda *a, **k: progress.append(a))
    monkeypatch.setattr(processing.meeting_store, "finish_processing", lambda mid, err=None: finished.append(err))

    async def disk_full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(asr_mod.asr_provider, "transcribe", disk_full)
    with pytest.raises(jobs.RetryLater):
        await processing.run("m1", Job())
    assert ("m1", "retrying") in progress and finished == []

    async def provider_down(*a, **k):
        raise asr_mod.ProviderError("upstream said: secret-internal-host 10.0.0.5 timeout")

    monkeypatch.setattr(asr_mod.asr_provider, "transcribe", provider_down)
    assert await processing.run("m1", None) is False  # job байхгүй → дахин оролдохгүй
    assert finished[-1].startswith("ASR_FAILED: ") and "10.0.0.5" not in finished[-1]


# ---------- ElevenLabs Scribe ----------

def _w(text, start, end, spk="speaker_0"):
    return {"type": "word", "text": text, "start": start, "end": end, "speaker_id": spk}


SP = {"type": "spacing", "text": " "}


@pytest.fixture
def scribe_key(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el-test")


def test_scribe_words_to_segments_keeps_meeting_wide_speakers():
    words = [
        _w("Сайн", 0.0, 0.5, "speaker_3"), SP, _w("байна", 0.6, 1.0, "speaker_3"), SP, _w("уу.", 1.0, 1.2, "speaker_3"),
        _w("Сайн.", 2.0, 2.5, "speaker_0"),
        {"type": "audio_event", "text": "(инээд)", "start": 3, "end": 4},
        _w("За", 31.0, 31.5, "speaker_3"), SP, _w("эхэлье.", 31.6, 32.0, "speaker_3"),
        _w("Тийм.", 40.0, 40.5, "speaker_0"),
    ]
    segs = asr_mod.scribe_words_to_segments(words, 30_000)
    assert [s.id for s in segs] == ["seg_1", "seg_2"]
    assert segs[0].text == "Яригч 1: Сайн байна уу.\nЯригч 2: Сайн."
    assert (segs[0].start_ms, segs[0].end_ms) == (0, 2500)
    # Хоёр дахь сегментэд ч мөн адил хүн мөн адил шошготой
    assert segs[1].text == "Яригч 1: За эхэлье.\nЯригч 2: Тийм." and segs[1].start_ms == 31_000


def test_scribe_long_monologue_is_split_at_word_boundary():
    words = []
    for i in range(100):  # 100 үг, 1с тутамд, цэггүй
        words += [_w(f"үг{i}", float(i), i + 0.5), SP]
    segs = asr_mod.scribe_words_to_segments(words, 30_000)
    assert len(segs) >= 2 and all(s.end_ms - s.start_ms <= 45_000 for s in segs)
    assert " ".join(s.text.replace("Яригч 1: ", "") for s in segs).split() == [f"үг{i}" for i in range(100)]


async def test_scribe_transcribe_sends_whole_file_and_saves_result(tmp_path, scribe_key, monkeypatch):
    audio = tmp_path / "uploaded.m4a"
    audio.write_bytes(b"fake-audio")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"text": "Сайн", "words": [_w("Сайн.", 0.0, 0.5)]})

    _patch_httpx(monkeypatch, handler)
    saved = {}
    ctx = asr_mod.ASRContext(hints=["Бат", "e-ID", "<bad>"], on_chunk=lambda k, v: saved.__setitem__(k, v),
                             prefilled=[{"start_ms": 0, "end_ms": 1000, "text": "шууд"}])
    segs = await asr_mod.ScribeASRProvider().transcribe(str(audio), context=ctx)
    assert [s.text for s in segs] == ["Яригч 1: Сайн."]
    assert len(seen) == 1 and seen[0].headers["xi-api-key"] == "el-test"
    body = seen[0].content.decode("utf-8", "replace")
    for part in ('name="diarize"', 'name="language_code"', 'name="keyterms"', "e-ID", "fake-audio"):
        assert part in body
    assert "<bad>" not in body
    assert saved[0]["segments"][0]["text"] == "Яригч 1: Сайн."

    # Дахин оролдлого: хадгалсан үр дүнг ашиглана, дахин төлөхгүй
    again = await asr_mod.ScribeASRProvider().transcribe(str(audio), context=asr_mod.ASRContext(resume=saved))
    assert [s.text for s in again] == ["Яригч 1: Сайн."] and len(seen) == 1


async def test_scribe_http_error_is_provider_error(tmp_path, scribe_key, monkeypatch):
    audio = tmp_path / "uploaded.m4a"
    audio.write_bytes(b"x")
    _patch_httpx(monkeypatch, lambda req: httpx.Response(401, text="invalid key"))
    with pytest.raises(asr_mod.ProviderError, match="401"):
        await asr_mod.ScribeASRProvider().transcribe(str(audio))


async def test_scribe_live_window_returns_plain_text(scribe_key, monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"text": " Сайн байна уу "})

    _patch_httpx(monkeypatch, handler)
    provider = asr_mod.ScribeASRProvider()
    assert asr_mod.supports_live(provider)
    assert await provider.transcribe_wav_bytes(b"RIFF....") == "Сайн байна уу"
    assert 'name="diarize"\r\n\r\nfalse' in seen["body"]


def test_scribe_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        asr_mod.ScribeASRProvider()


async def test_minutes_with_scribe_use_global_speaker_names(qwen_key, monkeypatch):
    monkeypatch.setattr(settings, "asr_provider", "scribe")
    seen = {}

    def handler(req):
        seen["system"] = json.loads(req.content)["messages"][0]["content"]
        return _chat({"title": "Х", "summary": "Т", "speaker_map": {"*": {"Яригч 2": "Бат"}}})

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(TRANSCRIPT)
    assert "already consistent across the WHOLE meeting" in seen["system"]
    assert "LOCALLY" not in seen["system"]
    assert m.speaker_map == {"seg_1": {"Яригч 2": "Бат"}, "seg_2": {"Яригч 2": "Бат"}}


# ---------- Qwen-ийн агуулгын шүүлтүүр (data_inspection_failed) ----------

FILTERED = httpx.Response(400, json={"error": {"code": "data_inspection_failed",
                                               "message": "Input text data may contain inappropriate content."}})
LONG = [TranscriptSegment(id=f"seg_{i}", start_ms=(i - 1) * 30_000, end_ms=i * 30_000,
                          text=("ХОРИОТОЙ үг" if i in (5, 6) else f"Ердийн яриа {i}.")) for i in range(1, 11)]


@pytest.fixture(autouse=True)
def _no_filter_delay(monkeypatch):
    monkeypatch.setattr(llm_mod, "FILTER_RETRY_DELAY_SEC", 0)


def _part_reply(body):
    """Хэсэг бүрт: тэр хэсэгт байгаа сегментүүдээс сэдэв гаргана."""
    text = body["messages"][-1]["content"]
    if text.startswith("Хэсэг 1:"):  # хураангуй нэгтгэх дуудлага
        return _chat({"summary": "Нэгтгэсэн."})
    ids = [f"seg_{i}" for i in range(1, 11) if f"[seg_{i} " in text]
    return _chat({"title": "Х", "summary": f"{ids[0]}-{ids[-1]}", "participants": ["Яригч 1"],
                  "topics": [{"title": ids[0], "source_segment_ids": ids}]})


async def test_flaky_filter_is_retried_without_splitting(qwen_key, monkeypatch):
    """Шүүлтүүр тогтворгүй: ижил хүсэлт эхэндээ татгалзагдаад дараа нь давдаг. Хуваахгүй, юу ч хасахгүй."""
    replies = iter([FILTERED, _chat({"title": "Х", "summary": "Бүтэн."})])
    _patch_httpx(monkeypatch, lambda req: next(replies))
    m = await llm_mod.QwenLLMProvider().generate_minutes(LONG)
    assert m.summary == "Бүтэн." and m.omitted_segment_ids == []


async def test_long_transcript_rejected_as_whole_is_split_and_merged(qwen_key, monkeypatch):
    """Бүтэн бичвэр байнга татгалзагдаж, хагас бүр давдаг (бодит хурал дээр ажигласан): юу ч хасахгүй, нэгтгэнэ."""
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if "[seg_1 " in body["messages"][-1]["content"] and "[seg_10 " in body["messages"][-1]["content"]:
            return FILTERED
        return _part_reply(body)

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(LONG)
    assert m.omitted_segment_ids == [] and m.summary == "Нэгтгэсэн."
    assert [t.title for t in m.topics] == ["seg_1", "seg_6"]
    assert m.topics[1].source_segment_ids == ["seg_6", "seg_7", "seg_8", "seg_9", "seg_10"]
    assert m.participants == ["Яригч 1"]
    assert len(calls) == llm_mod.FILTER_RETRIES + 2 + 1  # бүтэн ×3, хагас ×2, нэгтгэл ×1


async def test_segment_rejected_alone_is_omitted_and_reported(qwen_key, monkeypatch):
    def handler(req):
        body = json.loads(req.content)
        return FILTERED if "ХОРИОТОЙ" in body["messages"][-1]["content"] else _part_reply(body)

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(LONG)
    assert m.omitted_segment_ids == ["seg_5", "seg_6"]
    assert m.summary.startswith("Нэгтгэсэн.") and "02:00–03:00" in m.summary  # дараалсан 2 хэсэг нэг мужид
    covered = {i for t in m.topics for i in t.source_segment_ids}
    assert covered == {f"seg_{i}" for i in (1, 2, 3, 4, 7, 8, 9, 10)}


async def test_everything_rejected_fails_clearly(qwen_key, monkeypatch):
    _patch_httpx(monkeypatch, lambda req: FILTERED)
    with pytest.raises(ValueError, match="шүүлтүүр"):
        await llm_mod.QwenLLMProvider().generate_minutes(LONG[:2])


async def test_summary_only_is_split_on_filter(qwen_key, monkeypatch):
    def handler(req):
        text = json.loads(req.content)["messages"][-1]["content"]
        if "[seg_1 " in text and "[seg_10 " in text:
            return FILTERED
        return _chat({"summary": "Нэгтгэсэн." if text.startswith("Хэсэг 1:") else "Хэсэг."})

    _patch_httpx(monkeypatch, handler)
    assert await llm_mod.QwenLLMProvider().generate_summary(LONG) == "Нэгтгэсэн."


async def test_summary_merge_failure_falls_back_to_joining(qwen_key, monkeypatch):
    def handler(req):
        text = json.loads(req.content)["messages"][-1]["content"]
        if text.startswith("Хэсэг 1:") or ("[seg_1 " in text and "[seg_10 " in text):
            return FILTERED
        return _part_reply(json.loads(req.content))

    _patch_httpx(monkeypatch, handler)
    m = await llm_mod.QwenLLMProvider().generate_minutes(LONG)
    assert m.summary == "seg_1-seg_5 seg_6-seg_10"
