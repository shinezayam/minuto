"""Яригч ялгах, асуулт-хариулт, оролцогчид, байнгын оролцогчид, хөтлөгчийн нэр."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from app import db
from app.config import settings
from app.models.minutes import TranscriptSegment
from app.services import asr as asr_mod
from app.services import llm as llm_mod
from app.services.speakers import apply_speaker_map, parse_turns
from tests.test_providers import _chat, _patch_httpx, _sse, _upload_tone, needs_ffmpeg, qwen_key  # noqa: F401


# ---------- Задлах ----------

def test_parse_turns_labels_and_continuations():
    text = "Яригч 1: Сайн байна уу?\n**Speaker 2:** За, сайн.\nүргэлжлэл"
    assert parse_turns(text) == [("Яригч 1", "Сайн байна уу?"), ("Яригч 2", "За, сайн. үргэлжлэл")]


def test_parse_turns_ignores_colons_inside_sentences():
    assert parse_turns("Цаг 10:30-д уулзъя.") == [(None, "Цаг 10:30-д уулзъя.")]
    assert parse_turns("Тиймээс бид дараах зүйлийг шийдсэн: нэгдүгээрт") == [
        (None, "Тиймээс бид дараах зүйлийг шийдсэн: нэгдүгээрт")
    ]


def test_apply_speaker_map_unifies_labels_across_segments():
    segs = [
        TranscriptSegment(id="seg_1", start_ms=0, end_ms=30000, text="Яригч 1: Төсөв хэд вэ?\nЯригч 2: Арван сая."),
        # Хоёр дахь хэсэгт Omni дугаарыг дахин эхлүүлсэн: энд "Яригч 1" нь өмнөх "Яригч 2" (Бат)
        TranscriptSegment(id="seg_2", start_ms=30000, end_ms=60000, text="Яригч 1: Дараа нь тайлан өгнө."),
        TranscriptSegment(id="seg_3", start_ms=60000, end_ms=90000, text="шошгогүй бичвэр"),
    ]
    out = apply_speaker_map(segs, {"seg_1": {"Яригч 1": "Яригч 1", "Яригч 2": "Бат"}, "seg_2": {"Яригч 1": "Бат"}})

    assert [(t.speaker, t.text) for t in out[0].turns] == [("Яригч 1", "Төсөв хэд вэ?"), ("Бат", "Арван сая.")]
    assert out[0].speaker is None and out[0].text == "Яригч 1: Төсөв хэд вэ?\nБат: Арван сая."
    assert out[1].speaker == "Бат" and out[1].text == "Бат: Дараа нь тайлан өгнө."
    assert out[2].turns == [] and out[2].text == "шошгогүй бичвэр"


def test_apply_speaker_map_merges_adjacent_turns_of_same_speaker():
    seg = TranscriptSegment(id="seg_1", start_ms=0, end_ms=1, text="Яригч 1: А.\nЯригч 2: Б.")
    out = apply_speaker_map([seg], {"seg_1": {"Яригч 1": "Бат", "Яригч 2": "Бат"}})
    assert [(t.speaker, t.text) for t in out[0].turns] == [("Бат", "А. Б.")]


def test_unknown_segments_dropped_from_speaker_map():
    from app.models.minutes import MeetingMinutes

    m = MeetingMinutes(title="t", summary="s", speaker_map={"seg_1": {"Яригч 1": "А"}, "seg_99": {"Яригч 1": "Б"}})
    assert llm_mod.drop_unknown_sources(m, {"seg_1"}).speaker_map == {"seg_1": {"Яригч 1": "А"}}


# ---------- Оролцогчид, байнгын жагсаалт ----------

async def test_participants_saved_and_remembered_as_contacts(client):
    r = await client.post("/api/meetings", json={"title": "А", "participants": [" Бат ", "Сараа", "бат", ""]})
    assert r.status_code == 422  # хоосон нэр

    r = await client.post("/api/meetings", json={"title": "А", "participants": [" Бат ", "Сараа", "бат"]})
    assert r.status_code == 201
    mid = r.json()["id"]
    assert r.json()["participants"] == ["Бат", "Сараа"]  # давхардал, зай хасагдана

    r = await client.patch(f"/api/meetings/{mid}", json={"participants": ["Сараа", "Дорж"]})
    assert r.json()["participants"] == ["Сараа", "Дорж"]
    assert (await client.patch(f"/api/meetings/{mid}", json={"title": "Б"})).json()["participants"] == ["Сараа", "Дорж"]

    contacts = (await client.get("/api/contacts")).json()["contacts"]
    assert sorted(c["name"] for c in contacts) == ["Бат", "Дорж", "Сараа"]

    bat = next(c for c in contacts if c["name"] == "Бат")
    assert (await client.delete(f"/api/contacts/{bat['id']}")).status_code == 204
    assert (await client.delete(f"/api/contacts/{bat['id']}")).status_code == 404
    assert len((await client.get("/api/contacts")).json()["contacts"]) == 2


async def test_contacts_are_private(anon_client):
    from tests.conftest import register

    a = await register(anon_client)
    b = await register(anon_client)
    await anon_client.post(
        "/api/meetings", json={"participants": ["Нууц хүн"]}, headers={"Authorization": f"Bearer {a['token']}"}
    )
    r = await anon_client.get("/api/contacts", headers={"Authorization": f"Bearer {b['token']}"})
    assert r.json()["contacts"] == []
    a_contact = (await anon_client.get("/api/contacts", headers={"Authorization": f"Bearer {a['token']}"})).json()
    r = await anon_client.delete(
        f"/api/contacts/{a_contact['contacts'][0]['id']}", headers={"Authorization": f"Bearer {b['token']}"}
    )
    assert r.status_code == 404


# ---------- Хэрэглэгчийн нэр ----------

async def test_register_with_name_and_update(anon_client):
    r = await anon_client.post("/api/auth/register", json={"email": "n@test.mn", "password": "password123", "name": " Болд "})
    assert r.json()["user"]["name"] == "Болд"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = await anon_client.patch("/api/auth/me", json={"name": "Б. Болд"}, headers=headers)
    assert r.status_code == 200 and r.json()["name"] == "Б. Болд"
    assert (await anon_client.get("/api/auth/me", headers=headers)).json()["name"] == "Б. Болд"
    login = await anon_client.post("/api/auth/login", json={"email": "n@test.mn", "password": "password123"})
    assert login.json()["user"]["name"] == "Б. Болд"


def test_missing_columns_added_to_existing_database(tmp_path):
    """Хуучин (name, participants баганагүй) мэдээллийн сан дээр сервер эхлэхэд багана нэмэгдэнэ."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(320), password_hash VARCHAR(255), created_at DATETIME)"))
        conn.execute(text("CREATE TABLE meetings (id VARCHAR(36) PRIMARY KEY, owner_id VARCHAR(36), title VARCHAR(200))"))
        conn.execute(text("INSERT INTO users (id, email, password_hash) VALUES ('u1', 'old@test.mn', 'x')"))
    engine.dispose()

    db.configure(url)
    insp = inspect(db._engine)
    assert "name" in {c["name"] for c in insp.get_columns("users")}
    assert "participants" in {c["name"] for c in insp.get_columns("meetings")}
    assert "contacts" in insp.get_table_names()
    db.configure(url)  # дахин эхлэхэд алдаа гарахгүй


# ---------- Бүтэн урсгал ----------

@needs_ffmpeg
async def test_processing_labels_speakers_and_extracts_qa(client, tmp_path, qwen_key, monkeypatch):
    from app.services import processing

    monkeypatch.setattr(settings, "omni_chunk_sec", 10)
    monkeypatch.setattr(asr_mod, "asr_provider", asr_mod.QwenOmniASRProvider())
    monkeypatch.setattr(llm_mod, "llm_provider", llm_mod.QwenLLMProvider())
    await client.patch("/api/auth/me", json={"name": "Болд"})
    mid = await _upload_tone(client, tmp_path, 15)
    await client.patch(f"/api/meetings/{mid}", json={"participants": ["Бат", "Сараа"]})

    llm_requests: list = []
    asr_texts = iter(["Яригч 1: Тайлан хэзээ бэлэн болох вэ?\nЯригч 2: Баасан гаригт.", "Яригч 1: Би бэлдэнэ."])

    def handler(req):
        body = json.loads(req.content)
        if body.get("stream"):
            return httpx.Response(200, content=_sse(next(asr_texts)))
        llm_requests.append(body)
        return _chat(
            {
                "title": "Тайлан",
                "summary": "Тайлангийн хугацааг ярилцав.",
                "participants": ["Яригч 1", "Бат"],
                "qa": [
                    {
                        "question": "Тайлан хэзээ бэлэн болох вэ?",
                        "asked_by": "Яригч 1",
                        "answer": "Баасан гаригт.",
                        "answered_by": "Бат",
                        "source_segment_ids": ["seg_1", "seg_404"],
                    }
                ],
                "speaker_map": {"seg_1": {"Яригч 1": "Яригч 1", "Яригч 2": "Бат"}, "seg_2": {"Яригч 1": "Бат"}},
            }
        )

    _patch_httpx(monkeypatch, handler)
    assert (await client.post(f"/api/meetings/{mid}/process")).status_code == 202
    await processing.wait_all()

    m = (await client.get(f"/api/meetings/{mid}")).json()
    assert m["status"] == "completed", m
    # Оролцогчдын нэрс LLM-д очсон
    assert "Бат, Сараа" in llm_requests[0]["messages"][1]["content"]

    segs = (await client.get(f"/api/meetings/{mid}/transcript")).json()["segments"]
    assert [(t["speaker"], t["text"]) for t in segs[0]["turns"]] == [
        ("Яригч 1", "Тайлан хэзээ бэлэн болох вэ?"),
        ("Бат", "Баасан гаригт."),
    ]
    assert segs[1]["speaker"] == "Бат" and segs[1]["text"] == "Бат: Би бэлдэнэ."

    minutes = (await client.get(f"/api/meetings/{mid}/minutes")).json()
    assert minutes["prepared_by"] == "Minuto / Болд"
    assert minutes["participants"] == ["Яригч 1", "Бат"]
    assert minutes["qa"][0]["answered_by"] == "Бат"
    assert minutes["qa"][0]["source_segment_ids"] == ["seg_1"]
    assert minutes["speaker_map"] == {}  # бичвэрт хэрэглээд хадгалахгүй

    docx = await client.get(f"/api/meetings/{mid}/export.docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    import io

    from docx import Document

    body = "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    assert "Хурлын тэмдэглэл хөтөлсөн: Minuto / Болд" in body
    assert "Оролцогчид: Бат, Сараа" in body
    assert "Асуулт, хариулт" in body and "Хариулт (Бат): Баасан гаригт." in body
    assert "Бат: Би бэлдэнэ." in body


async def test_prepared_by_falls_back_to_email(client):
    me = (await client.get("/api/auth/me")).json()
    from app.services import meeting_store

    mid = (await client.post("/api/meetings", json={})).json()["id"]
    meeting_store.update_meeting_minutes(mid, {"title": "t", "summary": "s"})
    minutes = (await client.get(f"/api/meetings/{mid}/minutes")).json()
    assert minutes["prepared_by"] == f"Minuto / {me['email']}"
