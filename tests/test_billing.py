"""Billing: минут тоолох (off/shadow/enforce), багц, Wire төлбөр, webhook, Enterprise хүсэлт."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import struct
import threading
import time
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db import Meeting, PaymentOrder, Plan, Subscription, UsageEvent, session_scope
from app.services import billing, payments, processing
from app.services import upload_guard as guard
from tests.conftest import register

# ---------------------------------------------------------------- helpers


def wav_bytes(seconds: float = 0.5, rate: int = 8000, salt: int = 0) -> bytes:
    n = int(seconds * rate)
    data = struct.pack("<h", salt) + b"\x00\x00" * (n - 1)  # salt → өөр fingerprint
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )


@pytest.fixture
def duration(monkeypatch):
    """ffmpeg-ээс хамааралгүй: probe_duration_sec-ийн утгыг тест өөрөө тогтооно."""
    box = {"sec": 90.0}
    monkeypatch.setattr(guard, "probe_duration_sec", lambda path: box["sec"])
    return box


@pytest.fixture
def fake(monkeypatch):
    p = payments.FakeProvider()
    payments.set_provider(p)
    monkeypatch.setattr(settings, "billing_checkout_enabled", True)
    yield p
    payments.set_provider(None)


def metering(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "billing_metering", value)


async def upload_meeting(client: AsyncClient, salt: int = 0) -> str:
    mid = (await client.post("/api/meetings", json={"title": "Т"})).json()["id"]
    r = await client.post(f"/api/meetings/{mid}/upload", files={"file": ("a.wav", io.BytesIO(wav_bytes(salt=salt)), "audio/wav")})
    assert r.status_code == 200, r.text
    return mid


async def process(client: AsyncClient, mid: str):
    r = await client.post(f"/api/meetings/{mid}/process")
    await processing.wait_all()
    return r


def events(meeting_id: str | None = None) -> list[UsageEvent]:
    with session_scope() as s:
        q = select(UsageEvent).order_by(UsageEvent.created_at)
        if meeting_id:
            q = q.where(UsageEvent.meeting_id == meeting_id)
        return list(s.scalars(q).all())


def user_id_of(client) -> str:
    from app.services.auth import decode_token

    return decode_token(client.auth_token)


# ---------------------------------------------------------------- plans, rounding


def test_plans_match_the_approved_price_table():
    with session_scope() as s:
        rows = {p.code: (p.minutes, p.period_days, p.price_mnt, p.scope) for p in s.scalars(select(Plan)).all()}
    assert rows == {
        "free": (30, None, 0, "user"),
        "starter": (120, 30, 29900, "user"),
        "pro": (360, 30, 69900, "user"),
        "team": (900, 30, 149900, "workspace"),
        "enterprise": (None, None, None, "workspace"),
    }


@pytest.mark.parametrize("sec,minutes", [(0, 0), (None, 0), (0.1, 1), (59.9, 1), (60.0, 1), (60.1, 2), (3600, 60), (3600.4, 61)])
def test_rounding_is_ceil_per_meeting_with_minimum_one(sec, minutes):
    assert billing.billable_minutes(sec) == minutes


async def test_public_plans_endpoint(anon_client):
    r = await anon_client.get("/api/billing/plans")
    assert r.status_code == 200
    assert [p["code"] for p in r.json()["plans"]] == ["free", "starter", "pro", "team", "enterprise"]


# ---------------------------------------------------------------- off / shadow


async def test_off_mode_touches_nothing(client, duration, monkeypatch):
    metering(monkeypatch, "off")
    mid = await upload_meeting(client)
    assert (await process(client, mid)).status_code == 202
    assert events() == []
    me = (await client.get("/api/billing/me")).json()
    assert me["metering"] == "off" and me["minutes"]["remaining"] == 30


async def test_shadow_records_and_debits_after_transcript(client, duration, monkeypatch):
    metering(monkeypatch, "shadow")
    duration["sec"] = 125  # → 3 минут
    mid = await upload_meeting(client)
    assert (await process(client, mid)).status_code == 202
    [ev] = events(mid)
    assert (ev.state, ev.billable_minutes, ev.source, ev.shadow, ev.decision) == ("debited", 3, "upload", True, "allowed")
    me = (await client.get("/api/billing/me")).json()
    assert me["minutes"] == {"allowance": 30, "used": 3, "remaining": 27}


async def test_reprocessing_the_same_audio_is_free(client, duration, monkeypatch):
    metering(monkeypatch, "shadow")
    mid = await upload_meeting(client)
    await process(client, mid)
    await process(client, mid)
    assert len(events(mid)) == 1


async def test_shadow_never_blocks_and_marks_would_block(client, duration, monkeypatch):
    metering(monkeypatch, "shadow")
    duration["sec"] = 45 * 60  # 45 минут > 30 үнэгүй
    mid = await upload_meeting(client)
    assert (await process(client, mid)).status_code == 202
    [ev] = events(mid)

    assert ev.decision == "would_block" and ev.state == "debited"


async def test_shadow_billing_errors_never_break_processing(client, duration, monkeypatch):
    metering(monkeypatch, "shadow")

    def boom(*a, **k):
        raise RuntimeError("billing bug")

    monkeypatch.setattr(billing, "_reserve", boom)
    mid = await upload_meeting(client)
    assert (await process(client, mid)).status_code == 202
    assert (await client.get(f"/api/meetings/{mid}")).json()["status"] == "completed"


async def test_failed_transcription_releases_the_reservation(client, duration, monkeypatch):
    metering(monkeypatch, "shadow")
    from app.services import asr as asr_mod

    async def fail(*a, **k):
        raise ValueError("bad audio")

    monkeypatch.setattr(asr_mod.asr_provider, "transcribe", fail)
    mid = await upload_meeting(client)
    await process(client, mid)
    [ev] = events(mid)
    assert ev.state == "released"


# ---------------------------------------------------------------- enforce


async def test_enforce_refuses_an_upload_longer_than_what_is_left(client, duration, monkeypatch):
    metering(monkeypatch, "enforce")
    duration["sec"] = 31 * 60
    mid = await upload_meeting(client)
    r = await client.post(f"/api/meetings/{mid}/process")
    assert r.status_code == 402
    assert r.json()["detail"].startswith("MINUTES_INSUFFICIENT") and "31 минут" in r.json()["detail"]
    assert events(mid) == []
    assert (await client.get(f"/api/meetings/{mid}")).json()["status"] != "processing"


async def test_enforce_exhausted_then_blocked(client, duration, monkeypatch):
    metering(monkeypatch, "enforce")
    duration["sec"] = 30 * 60
    assert (await process(client, await upload_meeting(client, salt=1))).status_code == 202
    duration["sec"] = 10
    r = await process(client, await upload_meeting(client, salt=2))
    assert r.status_code == 402 and r.json()["detail"].startswith("MINUTES_EXHAUSTED")


async def test_enforce_live_recording_is_processed_with_overdraft(client, duration, monkeypatch):
    """Шууд бичлэгийг дахин бичих боломжгүй тул хэзээ ч түгжихгүй — нэг удаа хасах руу орно."""
    metering(monkeypatch, "enforce")
    duration["sec"] = 50 * 60
    mid = (await client.post("/api/meetings", json={"title": "Шууд"})).json()["id"]
    d = processing.meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "000000.wav").write_bytes(wav_bytes())  # шууд бичлэгийн хэсэг
    assert (await process(client, mid)).status_code == 202
    [ev] = events(mid)
    assert (ev.source, ev.decision, ev.billable_minutes) == ("live", "overdraft", 50)
    me = (await client.get("/api/billing/me")).json()
    assert me["minutes"]["remaining"] == -20


def test_concurrent_reserves_never_exceed_the_allowance(monkeypatch, duration):
    """10 зэрэг reserve (тус бүр 10 мин, нийт 30 мин үлдэгдэл): яг 3 нь л зөвшөөрөгдөнө."""
    metering(monkeypatch, "enforce")
    duration["sec"] = 600
    from app.db import User

    with session_scope() as s:
        u = User(email="c@t.mn", password_hash="x")
        s.add(u)
        s.flush()
        uid = u.id
        mids = []
        for i in range(10):
            m = Meeting(owner_id=uid, title=f"m{i}", transcript=[])
            s.add(m)
            s.flush()
            mids.append(m.id)
    for i, mid in enumerate(mids):
        d = processing.meeting_dir(mid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "uploaded.wav").write_bytes(wav_bytes(salt=i))

    results: list = []

    def go(mid):
        try:
            results.append(billing.reserve(mid, uid))
        except billing.BillingBlocked as e:
            results.append(e.code)

    threads = [threading.Thread(target=go, args=(m,)) for m in mids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if r not in ("MINUTES_EXHAUSTED", "MINUTES_INSUFFICIENT")) == 3
    with session_scope() as s:
        assert billing.balance(s, "user", uid).used == 30


def test_reconcile_releases_orphans(monkeypatch, duration):
    metering(monkeypatch, "shadow")
    with session_scope() as s:
        s.add(UsageEvent(scope_type="user", scope_id="u", meeting_id="gone", plan_code="free", source="upload",
                         duration_source="probe", billable_minutes=5, audio_fingerprint="f", state="reserved",
                         shadow=True, decision="allowed", created_at=billing._now() - timedelta(days=1)))
    assert billing.reconcile() == 1
    assert events()[0].state == "released"


# ---------------------------------------------------------------- checkout, payment


async def test_checkout_is_disabled_by_default(client):
    r = await client.post("/api/billing/checkout", json={"plan_code": "pro"})
    assert r.status_code == 403 and r.json()["detail"].startswith("BILLING_DISABLED")
    assert (await client.get("/api/billing/me")).json()["checkout_enabled"] is False


async def test_checkout_rejects_free_enterprise_and_unknown(client, fake):
    for code in ("free", "enterprise", "gold"):
        r = await client.post("/api/billing/checkout", json={"plan_code": code})
        assert r.status_code == 400, code


async def test_paid_order_activates_plan_once(client, fake, monkeypatch):
    r = await client.post("/api/billing/checkout", json={"plan_code": "pro"})
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["amount_mnt"] == 69900 and order["status"] == "pending" and order["payment"]["redirect_url"]
    ref = fake.calls and next(iter(fake.intents))

    assert (await client.get(f"/api/billing/orders/{order['id']}")).json()["status"] == "pending"
    fake.mark(ref, "paid")
    paid = (await client.get(f"/api/billing/orders/{order['id']}")).json()
    assert paid["status"] == "paid" and paid["payment"] is None

    me = (await client.get("/api/billing/me")).json()
    assert me["plan"]["code"] == "pro" and me["minutes"]["allowance"] == 360
    assert billing.apply_paid_order(order["id"]) is False  # давхар хэрэглэхгүй
    with session_scope() as s:
        assert len(s.scalars(select(Subscription)).all()) == 1


async def test_amount_mismatch_is_never_applied(client, fake):
    order = (await client.post("/api/billing/checkout", json={"plan_code": "starter"})).json()
    ref = next(iter(fake.intents))
    fake.mark(ref, "paid", amount=500)
    assert (await client.get(f"/api/billing/orders/{order['id']}")).json()["status"] == "pending"
    assert (await client.get("/api/billing/me")).json()["plan"]["code"] == "free"


async def test_other_users_cannot_see_an_order(client, anon_client, fake):
    order = (await client.post("/api/billing/checkout", json={"plan_code": "starter"})).json()
    other = await register(anon_client)
    r = await anon_client.get(f"/api/billing/orders/{order['id']}", headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 404


async def test_provider_outage_fails_the_order_cleanly(client, fake):
    fake.fail_create = payments.ProviderUnavailable("timeout")
    r = await client.post("/api/billing/checkout", json={"plan_code": "pro"})
    assert r.status_code == 503
    with session_scope() as s:
        assert s.scalars(select(PaymentOrder.status)).all() == ["failed"]


async def test_renewal_is_scheduled_after_the_current_period_and_cancel_keeps_access(client, fake):
    uid = user_id_of(client)
    for _ in range(2):
        order = (await client.post("/api/billing/checkout", json={"plan_code": "starter"})).json()
        fake.mark(f"pi_fake_{order['id'][:8]}", "paid")
        await client.get(f"/api/billing/orders/{order['id']}")
    with session_scope() as s:
        subs = s.scalars(select(Subscription).where(Subscription.user_id == uid).order_by(Subscription.period_start)).all()
        assert [x.status for x in subs] == ["active", "scheduled"]
        assert billing._aware(subs[1].period_start) == billing._aware(subs[0].period_end)
        later = billing._aware(subs[0].period_end) + timedelta(seconds=1)
        b = billing.balance(s, "user", uid, later)
        assert b.subscription.id == subs[1].id and b.allowance == 120 and b.used == 0  # үлдэгдэл шилжихгүй

    r = await client.post("/api/billing/cancel", json={})
    assert r.status_code == 200 and r.json()["cancel_at_period_end"] is True
    assert (await client.get("/api/billing/me")).json()["plan"]["code"] == "starter"


async def test_upgrade_takes_effect_immediately(client, fake):
    for code in ("starter", "pro"):
        order = (await client.post("/api/billing/checkout", json={"plan_code": code})).json()
        fake.mark(f"pi_fake_{order['id'][:8]}", "paid")
        await client.get(f"/api/billing/orders/{order['id']}")
    assert (await client.get("/api/billing/me")).json()["plan"]["code"] == "pro"


async def test_team_requires_an_owned_workspace(client, fake):
    r = await client.post("/api/billing/checkout", json={"plan_code": "team"})
    assert r.status_code == 422
    ws = (await client.post("/api/workspaces", json={"name": "Баг", "workspace_type": "organization"})).json()
    r = await client.post("/api/billing/checkout", json={"plan_code": "team", "workspace_id": ws["id"]})
    assert r.status_code == 201 and r.json()["amount_mnt"] == 149900


async def test_webhook_triggers_server_side_check(client, fake):
    order = (await client.post("/api/billing/checkout", json={"plan_code": "pro"})).json()
    ref = f"pi_fake_{order['id'][:8]}"
    # Биеэс итгэхгүй: provider "pending" гэж байхад webhook ирсэн ч идэвхжихгүй
    r = await client.post("/api/billing/webhooks/wire", content=json.dumps({"ref": ref}))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    fake.mark(ref, "paid")
    r = await client.post("/api/billing/webhooks/wire", content=json.dumps({"ref": ref}))
    assert r.json()["status"] == "paid"
    r = await client.post("/api/billing/webhooks/wire", content=json.dumps({"ref": "pi_unknown"}))
    assert r.status_code == 200 and r.json()["status"] is None


# ---------------------------------------------------------------- Wire adapter

# wire-go SDK-ийн testdata/webhook-signatures.json (албан ёсны тест векторууд)
_VEC_SECRET = "whsec_testvector000000000000000000"
_VEC_BODY = b'{"id":"evt_test","object":"event","type":"payment_intent.succeeded"}'


@pytest.mark.parametrize("name,body,header,valid", [
    ("valid", _VEC_BODY, "t=1700000100,v1=a7b1a9325beb479412e4a7bc529a94fefebbc9530e56f33ba9c36825e238dc15", True),
    ("tampered_body", _VEC_BODY + b" ", "t=1700000100,v1=a7b1a9325beb479412e4a7bc529a94fefebbc9530e56f33ba9c36825e238dc15", False),
    ("tampered_sig", _VEC_BODY, "t=1700000100,v1=" + "0" * 64, False),
    ("expired", _VEC_BODY, "t=1699999000,v1=1955a9d48f404e643184b5b21862ef8439d4fbddc5dcfe001d43beeb2654aea3", False),
    ("malformed_header", _VEC_BODY, "garbage", False),
])
def test_wire_signature_matches_official_vectors(name, body, header, valid):
    if valid:
        assert payments.verify_wire_signature(body, header, _VEC_SECRET, now=1700000300)["type"] == "payment_intent.succeeded"
    else:
        with pytest.raises(payments.InvalidSignature):
            payments.verify_wire_signature(body, header, _VEC_SECRET, now=1700000300)


def _sign(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def test_wire_provider_hosted_checkout_check_and_webhook():
    """docs.wire.mn: PaymentIntent → /v1/checkout/sessions (url) — /confirm дуудахгүй."""
    seen: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.headers.get("authorization"), req.headers.get("idempotency-key")))
        if req.url.path == "/v1/payment_intents" and req.method == "POST":
            body = json.loads(req.content)
            assert body["amount"] == 69900 and body["currency"] == "MNT" and body["metadata"]["order_id"] == "ord-1"
            assert req.headers["content-type"].startswith("application/json")
            return httpx.Response(200, json={"id": "pi_1", "amount": 69900, "status": "requires_payment_method"})
        if req.url.path == "/v1/checkout/sessions":
            body = json.loads(req.content)
            assert body["payment_intent"] == "pi_1" and body["success_url"].endswith("?order=ord-1")
            return httpx.Response(200, json={"id": "cs_1", "object": "checkout.session", "url": "https://pay.wire.mn/c/cs_1", "status": "open"})
        if req.url.path == "/v1/payment_intents/pi_1":
            return httpx.Response(200, json={"id": "pi_1", "amount": 69900, "currency": "MNT", "status": "succeeded",
                                             "metadata": {"order_id": "ord-1"}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    p = payments.WireProvider("sk_live_x", "whsec_abc", transport=httpx.MockTransport(handler))
    created = p.create_payment(payments.OrderRef("ord-1", 69900, "Minuto Pro", "https://app.example/billing/return?order=ord-1"))
    assert created.provider_ref == "pi_1" and created.payment.redirect_url == "https://pay.wire.mn/c/cs_1"
    assert [x[1] for x in seen] == ["/v1/payment_intents", "/v1/checkout/sessions"]
    assert seen[0][2] == "Bearer sk_live_x" and seen[0][3] == "minuto-ord-1" and seen[1][3] == "minuto-ord-1-checkout"

    check = p.check_payment("pi_1")
    assert (check.status, check.amount_mnt, check.order_id) == ("paid", 69900, "ord-1")

    body = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_1"}}}).encode()
    assert p.parse_webhook(body, {"wirepayment-signature": _sign(body, "whsec_abc")}) == "pi_1"
    with pytest.raises(payments.InvalidSignature):
        p.parse_webhook(body, {"wirepayment-signature": _sign(body, "whsec_wrong")})
    ping = json.dumps({"id": "evt_2", "type": "endpoint.verification", "data": {"object": {}}}).encode()
    assert p.parse_webhook(ping, {"wirepayment-signature": _sign(ping, "whsec_abc")}) is None  # verify ping → 200


def test_documented_qr_next_action_shape():
    info = payments.payment_info_from_next_action(
        {"type": "qr", "qr": {"text": "000201010212", "deeplinks": [{"name": "Khan bank", "link": "khanbank://q?qPay_QRcode=1"}]}})
    assert info.qr_text == "000201010212" and info.deep_links[0]["name"] == "Khan bank" and info.redirect_url is None


async def test_verification_ping_gets_200(client, monkeypatch):
    payments.set_provider(payments.WireProvider("k", "whsec_abc", transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    try:
        ping = json.dumps({"id": "evt_2", "type": "endpoint.verification", "data": {"object": {}}}).encode()
        r = await client.post("/api/billing/webhooks/wire", content=ping, headers={"WirePayment-Signature": _sign(ping, "whsec_abc")})
        assert r.status_code == 200
    finally:
        payments.set_provider(None)


def test_wire_provider_maps_errors():
    p5 = payments.WireProvider("k", "s", transport=httpx.MockTransport(lambda r: httpx.Response(503)), max_retries=0)
    with pytest.raises(payments.ProviderUnavailable):
        p5.check_payment("pi_1")
    p4 = payments.WireProvider("k", "s", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"code": "bad_key"}})))
    with pytest.raises(payments.ProviderRejected):
        p4.check_payment("pi_1")


def test_redirect_next_action_and_unsafe_urls():
    info = payments.payment_info_from_next_action({"type": "redirect_to_url", "redirect_to_url": {"url": "https://pay.wire.mn/x"}})
    assert info.redirect_url == "https://pay.wire.mn/x"
    assert payments.payment_info_from_next_action({"redirect_to_url": {"url": "javascript:alert(1)"}}).redirect_url is None


async def test_webhook_with_real_provider_rejects_bad_signature(client, monkeypatch):
    payments.set_provider(payments.WireProvider("k", "whsec_abc", transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    try:
        r = await client.post("/api/billing/webhooks/wire", content=b"{}", headers={"WirePayment-Signature": "t=1,v1=00"})
        assert r.status_code == 400
    finally:
        payments.set_provider(None)


# ---------------------------------------------------------------- contact


async def test_contact_request_is_stored_and_validated(anon_client):
    ok = await anon_client.post("/api/contact-requests", json={"name": "Бат", "email": "bat@co.mn", "company": "Co", "message": "50 хэрэглэгч"})
    assert ok.status_code == 201 and ok.json()["id"]
    bad = await anon_client.post("/api/contact-requests", json={"name": "Бат", "email": "not-an-email", "message": "x"})
    assert bad.status_code == 422
