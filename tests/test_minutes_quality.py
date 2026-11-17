"""4.3: нотолгоо (evidence) шалгалт, шийдвэрийн төлөв, хүний шалгалт/батлалт, экспортын тэмдэглэгээ, зөвхөн-хураангуй дуудлага."""
from __future__ import annotations

import io
import json

import httpx
import pytest
from docx import Document
from httpx import AsyncClient

from app.models.minutes import MeetingMinutes, TranscriptSegment
from app.services import llm as llm_mod
from app.services import meeting_store
from app.services.minutes_verify import normalize, quote_in_segment, verify_minutes
from tests.conftest import register
from tests.test_providers import _chat, _patch_httpx, qwen_key  # noqa: F401

SEGS = [
    TranscriptSegment(id="seg_1", start_ms=0, end_ms=1000, text="Бат: Бид шинэ хувилбарыг Баасан гарагт гаргахаар шийдлээ."),
    TranscriptSegment(id="seg_2", start_ms=1000, end_ms=2000, text="Сараа: Магадгүй үнийг бууруулж болох юм."),
    TranscriptSegment(id="seg_3", start_ms=2000, end_ms=3000, text="Дорж: Тайлангаа Даваа гарагт бэлдэнэ."),
]


def _minutes(**over) -> MeetingMinutes:
    data = {
        "title": "Т", "summary": "С",
        "decisions": [
            {"text": "Баасанд гаргана", "status": "confirmed", "confidence": 0.9,
             "evidence": [{"segment_id": "seg_1", "quote": "шинэ хувилбарыг баасан гарагт гаргахаар"}], "source_segment_ids": ["seg_1"]},
            {"text": "Үнийг бууруулна", "status": "confirmed",
             "evidence": [{"segment_id": "seg_2", "quote": "үнийг 50% бууруулна гэж тохирсон"}], "source_segment_ids": ["seg_2"]},
            {"text": "Нотолгоогүй", "status": "confirmed", "source_segment_ids": ["seg_1"]},
            {"text": "Санал", "status": "proposed",
             "evidence": [{"segment_id": "seg_2", "quote": "үнийг бууруулж болох юм"}]},
        ],
        "action_items": [
            {"task": "Тайлан", "owner": "Дорж", "evidence": [{"segment_id": "seg_3", "quote": "Тайлангаа Даваа гарагт бэлдэнэ"}]},
            {"task": "Хуурамч", "evidence": [{"segment_id": "seg_99", "quote": "ийм сегмент байхгүй"}]},
        ],
    }
    data.update(over)
    return MeetingMinutes(**data)


def test_normalize_and_quote_matching():
    assert normalize("  Тайлан,  БЭЛДЭНЭ!! ") == "тайлан бэлдэнэ"
    assert quote_in_segment("шинэ хувилбарыг баасан гарагт", SEGS[0].text)
    assert not quote_in_segment("огт өөр өгүүлбэр байна", SEGS[0].text)
    assert not quote_in_segment("бат", SEGS[0].text)  # хэт богино ишлэл санамсаргүй таарч болно


def test_verification_flags_each_case_and_downgrades_unproven_confirmed():
    m = verify_minutes(_minutes(), SEGS)
    d = m.decisions
    assert d[0].verification == "quote_found" and d[0].status == "confirmed"
    assert d[1].verification == "quote_not_found" and d[1].status == "unclear"  # зохиосон ишлэл → батлагдсан гэж үзэхгүй
    assert d[2].verification == "no_evidence" and d[2].status == "unclear"  # сегментийн id л байгаа нь баталгаа биш
    assert d[3].verification == "quote_found" and d[3].status == "proposed"  # санал нь шийдвэр болохгүй
    assert [a.verification for a in m.action_items] == ["quote_found", "quote_not_found"]  # байхгүй сегмент рүү заасан
    assert [x.needs_review for x in d] == [False, True, True, True]
    assert [a.needs_review for a in m.action_items] == [False, True]


def test_legacy_minutes_without_new_fields_still_load():
    m = MeetingMinutes(**{"title": "Х", "summary": "С", "decisions": [{"text": "Хуучин"}], "action_items": [{"task": "т"}]})
    assert m.decisions[0].status == "unclear" and m.decisions[0].verification == "no_evidence"
    assert m.minutes_status == "draft" and m.decisions[0].needs_review is True


async def test_qwen_pipeline_verifies_and_drops_hallucinated_sources(qwen_key, monkeypatch):
    payload = {
        "title": "Т", "summary": "С",
        "decisions": [
            {"text": "Баасанд гаргана", "status": "confirmed", "confidence": 0.8,
             "evidence": [{"segment_id": "seg_1", "quote": "Баасан гарагт гаргахаар шийдлээ"}], "source_segment_ids": ["seg_1", "seg_77"]},
            {"text": "Зохиомол", "status": "confirmed", "evidence": [{"segment_id": "seg_2", "quote": "энэ үг сегментэд байхгүй"}]},
        ],
    }
    _patch_httpx(monkeypatch, lambda req: _chat(payload))
    m = await llm_mod.QwenLLMProvider().generate_minutes(SEGS)
    assert m.decisions[0].verification == "quote_found" and m.decisions[0].source_segment_ids == ["seg_1"]
    assert m.decisions[1].verification == "quote_not_found" and m.decisions[1].status == "unclear"


async def test_prompt_demands_status_and_verbatim_evidence(qwen_key, monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["system"] = json.loads(req.content)["messages"][0]["content"]
        return _chat({"title": "Т", "summary": "С"})

    _patch_httpx(monkeypatch, handler)
    await llm_mod.QwenLLMProvider().generate_minutes(SEGS)
    s = seen["system"]
    assert '"confirmed" ONLY if' in s and "VERBATIM" in s and "never" in s.lower()


# ---------- Төлөв, хүний шалгалт ----------

async def _stored(client: AsyncClient, **minutes_over) -> str:
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    meeting_store.update_meeting_transcript(mid, [s.model_dump() for s in SEGS])
    meeting_store.update_meeting_minutes(mid, verify_minutes(_minutes(**minutes_over), SEGS).model_dump(exclude={"prepared_by"}))
    return mid


async def test_minutes_start_as_draft_and_expose_flags(client: AsyncClient):
    mid = await _stored(client)
    m = (await client.get(f"/api/meetings/{mid}/minutes")).json()
    assert m["minutes_status"] == "draft" and m["approved_by"] is None
    assert m["decisions"][1]["needs_review"] is True and m["decisions"][0]["verification"] == "quote_found"


async def test_item_review_accept_edit_reject(client: AsyncClient):
    mid = await _stored(client)
    put = lambda kind, i, body: client.put(f"/api/meetings/{mid}/minutes/{kind}/{i}/review", json=body)  # noqa: E731
    r = await put("decisions", 1, {"decision": "accepted"})
    d = r.json()["minutes"]["decisions"][1]
    assert r.status_code == 200 and d["review"] == "accepted" and d["status"] == "confirmed" and d["needs_review"] is False
    r = await put("decisions", 3, {"text": "Үнийг 10% бууруулна"})
    d = r.json()["minutes"]["decisions"][3]
    assert d["review"] == "edited" and d["text"] == "Үнийг 10% бууруулна" and d["status"] == "confirmed"
    r = await put("action_items", 1, {"decision": "rejected"})
    assert r.json()["minutes"]["action_items"][1]["review"] == "rejected"
    assert (await put("decisions", 0, {"decision": "rejected"})).json()["minutes"]["decisions"][0]["status"] == "rejected"
    # буруу оролт
    assert (await put("decisions", 99, {"decision": "accepted"})).status_code == 404
    assert (await put("topics", 0, {"decision": "accepted"})).status_code == 422
    assert (await put("decisions", 0, {"decision": "maybe"})).status_code == 422
    assert (await put("decisions", 0, {"text": "   "})).status_code == 422


async def test_approval_flow_and_automatic_invalidation(client: AsyncClient):
    mid = await _stored(client)
    status = lambda s: client.put(f"/api/meetings/{mid}/minutes/status", json={"status": s})  # noqa: E731
    assert (await status("in_review")).json()["minutes_status"] == "in_review"
    r = await status("approved")
    body = r.json()
    assert r.status_code == 200 and body["minutes_status"] == "approved" and body["approved_by"] and body["approved_at"]
    # Агуулга өөрчлөгдвөл батлалт цуцлагдана (action items, шийдвэрийн засвар)
    await client.put(f"/api/meetings/{mid}/action-items", json={"action_items": [{"task": "Тайлан", "owner": "Дорж"}]})
    assert (await client.get(f"/api/meetings/{mid}/minutes")).json()["minutes_status"] == "in_review"
    await status("approved")
    await client.put(f"/api/meetings/{mid}/minutes/decisions/0/review", json={"text": "Өөрчилсөн"})
    assert (await client.get(f"/api/meetings/{mid}/minutes")).json()["minutes_status"] == "in_review"
    assert (await status("bogus")).status_code == 422


async def test_action_item_update_keeps_evidence_of_unchanged_tasks(client: AsyncClient):
    mid = await _stored(client)
    items = (await client.get(f"/api/meetings/{mid}/minutes")).json()["action_items"]
    r = await client.put(f"/api/meetings/{mid}/action-items", json={"action_items": [{"task": "Тайлан", "owner": "Дорж", "status": "done"}]})
    assert r.status_code == 200
    kept = (await client.get(f"/api/meetings/{mid}/minutes")).json()["action_items"][0]
    assert kept["status"] == "done" and kept["verification"] == "quote_found" and kept["evidence"] == items[0]["evidence"]


async def test_only_owner_or_admin_can_approve_in_a_workspace(client: AsyncClient, anon_client: AsyncClient):
    owner_ws = (await client.post("/api/workspaces", json={"name": "Org"})).json()["id"]
    editor = await register(anon_client, "ed-approve@org.mn")
    await client.post(f"/api/workspaces/{owner_ws}/invite", json={"email": "ed-approve@org.mn", "role": "editor"})
    mid = (await client.post("/api/meetings", json={"title": "Т", "workspace_id": owner_ws})).json()["id"]
    meeting_store.update_meeting_minutes(mid, _minutes().model_dump(exclude={"prepared_by"}))
    h = {"Authorization": f"Bearer {editor['token']}"}
    async with client.__class__(transport=client._transport, base_url="http://test") as ed:
        assert (await ed.put(f"/api/meetings/{mid}/minutes/status", json={"status": "in_review"}, headers=h)).status_code == 200
        r = await ed.put(f"/api/meetings/{mid}/minutes/status", json={"status": "approved"}, headers=h)
        assert r.status_code == 403 and r.json()["detail"].startswith("FORBIDDEN_APPROVE")
    assert (await client.put(f"/api/meetings/{mid}/minutes/status", json={"status": "approved"})).status_code == 200


# ---------- Экспорт ----------

def _docx_text(content: bytes) -> str:
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs)


async def test_draft_export_carries_notice_and_separates_proposals(client: AsyncClient):
    mid = await _stored(client)
    text = _docx_text((await client.get(f"/api/meetings/{mid}/export.docx")).content)
    assert "AI автоматаар үүсгэсэн" in text and "Төлөв: Ноорог" in text
    assert "Санал, тодорхойгүй зүйлс (шийдвэр биш)" in text
    assert "[Санал] Санал" in text
    # зөвхөн нотолгоотой, батлагдсан шийдвэр "Шийдвэрүүд" хэсэгт
    head, _, tail = text.partition("Санал, тодорхойгүй зүйлс")
    assert "Баасанд гаргана" in head and "Баасанд гаргана" not in tail


async def test_approved_export_has_no_draft_notice(client: AsyncClient):
    mid = await _stored(client)
    await client.put(f"/api/meetings/{mid}/minutes/status", json={"status": "approved"})
    text = _docx_text((await client.get(f"/api/meetings/{mid}/export.docx")).content)
    assert "Батлагдсан:" in text and "AI автоматаар үүсгэсэн" not in text
    assert (await client.get(f"/api/meetings/{mid}/export.pdf")).status_code == 200


# ---------- F-24: зөвхөн хураангуй ----------

async def test_regenerate_summary_is_a_summary_only_call(client: AsyncClient, monkeypatch):
    calls = {"minutes": 0, "summary": 0}

    class Fake:
        name = "fake"

        async def generate_minutes(self, *a, **k):
            calls["minutes"] += 1
            raise AssertionError("бүтэн минутын pipeline дуудагдах ёсгүй")

        async def generate_summary(self, transcript, language="mn"):
            calls["summary"] += 1
            return "Шинэ хураангуй"

    monkeypatch.setattr(llm_mod, "llm_provider", Fake())
    mid = await _stored(client)
    before = (await client.get(f"/api/meetings/{mid}/minutes")).json()
    r = await client.post(f"/api/meetings/{mid}/regenerate-summary")
    assert r.status_code == 202 and r.json()["summary"] == "Шинэ хураангуй"
    assert calls == {"minutes": 0, "summary": 1}
    after = (await client.get(f"/api/meetings/{mid}/minutes")).json()
    assert after["summary"] == "Шинэ хураангуй"
    assert after["decisions"] == before["decisions"] and after["action_items"] == before["action_items"]  # бусад хэсэг хөндөгдөөгүй


async def test_qwen_summary_only_prompt_is_short(qwen_key, monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return _chat({"summary": "Товч агуулга."})

    _patch_httpx(monkeypatch, handler)
    out = await llm_mod.QwenLLMProvider().generate_summary(SEGS, language="en")
    assert out == "Товч агуулга."
    system = seen["body"]["messages"][0]["content"]
    assert len(system) < len(llm_mod.SYSTEM_PROMPT) / 2 and "English" in system


def test_pdf_export_handles_cyrillic_rationale_and_many_sections():
    """Өмнө нь кирилл бичвэрт exception шиддэг (Helvetica), шийдвэрийн дараа шалтгаан бичихэд "horizontal space" алдаа гардаг байсан."""
    from app.services.export_pdf import generate_meeting_pdf

    m = MeetingMinutes(
        title="Бүтээгдэхүүний хурал", summary="Товч агуулга " * 30,
        topics=[{"title": "Сэдэв", "description": "Тайлбар " * 40}],
        decisions=[
            {"text": "Баасан гарагт гаргана", "rationale": "Шалтгаан урт " * 20, "status": "confirmed"},
            {"text": "Үнийг бууруулах санал", "status": "proposed"},
        ],
        action_items=[{"task": "Тайлан бэлдэх " * 8, "owner": "Дорж", "due_date": "2026-12-01"}],
        open_questions=[{"text": "Төсөв хэд вэ?"}],
        participants=["Бат", "Сараа"],
    )
    pdf = generate_meeting_pdf("x", "Хурал", "2026-11-16T10:00:00Z", m, [{"start_ms": 61000, "speaker": "Бат", "text": "Сайн байна уу"}])
    assert pdf[:5] == b"%PDF-" and b"DejaVu" in pdf and len(pdf) > 10_000  # Unicode фонт шигтгэгдсэн


async def test_ask_context_loading_is_bounded(client: AsyncClient):
    uid = (await client.get("/api/auth/me")).json()["id"]
    for i in range(5):
        mid = (await client.post("/api/meetings", json={"title": f"Т{i}"})).json()["id"]
        meeting_store.update_meeting_transcript(mid, [{"id": "seg_1", "start_ms": 0, "end_ms": 1, "text": "x"}])
    assert len(meeting_store.list_meeting_contents(uid, limit=3)) == 3
    assert len(meeting_store.list_meeting_contents(uid)) == 5


def test_pdf_official_format_helpers():
    """Албан хэлбэр: огноо Улаанбаатарын цагаар (UTC+8), тооны нөхцөл зөв; бичвэр PDF-д орохгүй ч алдаа гаргахгүй."""
    from app.services.export_pdf import _date_mn, _duration_mn, _genitive, _local_dt, generate_meeting_pdf

    dt = _local_dt("2026-11-17T03:15:00")  # DB-ийн UTC
    assert (dt.hour, dt.minute) == (11, 15)
    assert _date_mn(dt) == "2026 оны 11 сарын 17-ны өдөр"
    assert [_genitive(n) for n in (1, 4, 9, 11, 17, 20, 21)] == ["1-ний", "4-ний", "9-ний", "11-ний", "17-ны", "20-ны", "21-ний"]
    assert _duration_mn(47 * 60000) == "47 минут" and _duration_mn(65 * 60000) == "1 цаг 5 минут"
    empty = generate_meeting_pdf("y", "test", "2026-11-17T03:15:00", MeetingMinutes(title="test", summary=""), None, [])
    assert empty[:5] == b"%PDF-"
