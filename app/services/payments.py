"""Төлбөрийн provider-ийн давхарга. Minuto-ийн бусад код HTTP-ээр provider-тэй ярихгүй, зөвхөн энэ interface-ээр.

Wire (https://wire.mn, Buildry): QPay зэрэг операторуудыг нэгтгэсэн, Stripe-тэй төстэй API.
  * POST /v1/payment_intents {amount (бүхэл төгрөг), currency, metadata}  + Idempotency-Key
  * POST /v1/payment_intents/{id}/confirm {return_url}  → next_action (QR / redirect)
  * GET  /v1/payment_intents/{id}  → status (succeeded = төлөгдсөн)
  * Webhook: header `WirePayment-Signature: t=<unix>,v1=<hex>`, HMAC-SHA256(secret, "<t>.<raw body>"), 300 сек tolerance
    (албан ёсны wire-go SDK-ийн webhook.go болон testdata/webhook-signatures.json-оос).
Webhook-ийн биеэс ХЭЗЭЭ Ч төлөв авахгүй: зөвхөн аль intent гэдгийг мэдээд, сервер талаас GET-ээр дахин шалгана.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "WirePayment-Signature"
SIGNATURE_TOLERANCE_SEC = 300

Status = Literal["pending", "paid", "failed", "expired", "cancelled"]


class ProviderUnavailable(Exception):
    """Сүлжээ, timeout, 5xx, 429 — түр алдаа, дахин оролдож болно."""


class ProviderRejected(Exception):
    """4xx: түлхүүр буруу, дүн буруу гэх мэт — дахин оролдоод нэмэргүй."""


class InvalidSignature(Exception):
    pass


@dataclass(frozen=True)
class OrderRef:
    order_id: str
    amount_mnt: int
    description: str
    return_url: str
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentInfo:
    redirect_url: Optional[str] = None
    qr_text: Optional[str] = None
    qr_image: Optional[str] = None
    deep_links: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"redirect_url": self.redirect_url, "qr_text": self.qr_text, "qr_image": self.qr_image, "deep_links": list(self.deep_links)}


@dataclass(frozen=True)
class CreatedPayment:
    provider_ref: str
    payment: PaymentInfo
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class PaymentCheck:
    status: Status
    amount_mnt: Optional[int]
    currency: Optional[str]
    order_id: Optional[str]  # metadata.order_id — бидний өгсөнтэй таарах ёстой


class PaymentProvider(Protocol):
    name: str

    def create_payment(self, order: OrderRef) -> CreatedPayment: ...
    def check_payment(self, provider_ref: str) -> PaymentCheck: ...
    def parse_webhook(self, body: bytes, headers: Dict[str, str]) -> Optional[str]: ...  # provider_ref эсвэл None


# ---------------------------------------------------------------- Wire

_PENDING = {"requires_payment_method", "requires_confirmation", "requires_action", "processing", "pending"}
_STATUS_MAP: Dict[str, Status] = {"succeeded": "paid", "canceled": "cancelled", "cancelled": "cancelled", "failed": "failed", "expired": "expired"}


def verify_wire_signature(body: bytes, header: Optional[str], secret: str, now: Optional[float] = None,
                          tolerance: int = SIGNATURE_TOLERANCE_SEC) -> dict:
    """wire-go Webhooks.Verify-тэй ижил. Амжилттай бол event (dict), үгүй бол InvalidSignature."""
    if not header or not secret:
        raise InvalidSignature("missing")
    ts: Optional[int] = None
    v1: Optional[str] = None
    for part in header.split(","):
        k, sep, val = part.strip().partition("=")
        if not sep:
            continue
        if k == "t":
            try:
                ts = int(val)
            except ValueError:
                raise InvalidSignature("bad timestamp") from None
        elif k == "v1":
            v1 = val
    if not ts or not v1:
        raise InvalidSignature("malformed")
    now = time.time() if now is None else now
    if abs(int(now) - ts) > tolerance:
        raise InvalidSignature("expired")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise InvalidSignature("mismatch")
    try:
        return json.loads(body)
    except ValueError:
        raise InvalidSignature("bad json") from None


def _find(obj: Any, keys: tuple, depth: int = 0) -> Any:
    """next_action-ийн бүтэц оператороос хамаарч өөр байж болох тул нэрээр нь гүн хайна."""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _find(v, keys, depth + 1)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, keys, depth + 1)
            if found not in (None, "", [], {}):
                return found
    return None


def payment_info_from_next_action(next_action: Any) -> PaymentInfo:
    if not next_action:
        return PaymentInfo()
    redirect = _find(next_action, ("url", "redirect_url", "hosted_url", "payment_url", "checkout_url", "short_url"))
    # docs.wire.mn: {"type": "qr", "qr": {"text": "...", "deeplinks": [{name, link}]}}
    qr_obj = next_action.get("qr") if isinstance(next_action, dict) else None
    qr_text = qr_obj.get("text") if isinstance(qr_obj, dict) else None
    qr_text = qr_text or _find(next_action, ("qr_text", "qr_code"))
    qr_image = _find(next_action, ("qr_image", "qr_image_b64", "qr_png"))
    links_raw = _find(next_action, ("deep_links", "deeplinks", "urls", "bank_links"))
    links: List[dict] = []
    if isinstance(links_raw, list):
        for item in links_raw[:30]:
            if isinstance(item, dict) and isinstance(item.get("link"), str):
                links.append({"name": str(item.get("name") or item.get("description") or "Bank")[:60],
                              "link": item["link"], "logo": item.get("logo") if isinstance(item.get("logo"), str) else None})
    if isinstance(qr_image, str) and qr_image.startswith("data:"):
        qr_image = qr_image.split(",", 1)[-1]
    return PaymentInfo(
        redirect_url=redirect if isinstance(redirect, str) and redirect.startswith("https://") else None,
        qr_text=qr_text if isinstance(qr_text, str) else None,
        qr_image=qr_image if isinstance(qr_image, str) else None,
        deep_links=links,
    )


class WireProvider:
    name = "wire"

    def __init__(self, api_key: str, webhook_secret: str, base_url: str = "https://api.wire.mn",
                 transport: Optional[httpx.BaseTransport] = None, max_retries: int = 2) -> None:
        self._secret = webhook_secret
        self._retries = max_retries
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )

    def _request(self, method: str, path: str, body: Optional[dict] = None, idem: Optional[str] = None) -> dict:
        headers = {"Idempotency-Key": idem} if idem else {}
        for attempt in range(self._retries + 1):
            try:
                r = self._client.request(method, path, json=body, headers=headers)
            except httpx.HTTPError as e:
                if attempt < self._retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise ProviderUnavailable(type(e).__name__) from None
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < self._retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise ProviderUnavailable(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                code = ""
                try:
                    code = str((r.json().get("error") or {}).get("code") or "")
                except Exception:  # noqa: BLE001
                    pass
                # Хариуны бие/түлхүүрийг лог руу бичихгүй — зөвхөн статус, код
                raise ProviderRejected(f"HTTP {r.status_code} {code}".strip())
            return r.json()
        raise ProviderUnavailable("retries exhausted")  # pragma: no cover

    def create_payment(self, order: OrderRef) -> CreatedPayment:
        """PaymentIntent + Hosted checkout session (docs.wire.mn/docs/guides/hosted-checkout).
        Худалдан авагч pay.wire.mn/c/... дээр QPay болон бусад операторыг сонгож төлнө. /confirm дуудахгүй (docs-ийн дагуу)."""
        meta = {"order_id": order.order_id, **{k[:40]: str(v)[:500] for k, v in order.metadata.items()}}
        pi = self._request("POST", "/v1/payment_intents",
                           {"amount": order.amount_mnt, "currency": "MNT", "description": order.description[:200], "metadata": meta},
                           idem=f"minuto-{order.order_id}")
        pi_id = pi.get("id")
        if not isinstance(pi_id, str) or not pi_id:
            raise ProviderRejected("no payment intent id")
        if int(pi.get("amount") or -1) != order.amount_mnt:
            raise ProviderRejected("amount mismatch on create")
        session = self._request("POST", "/v1/checkout/sessions",
                                {"payment_intent": pi_id, "success_url": order.return_url, "cancel_url": order.return_url},
                                idem=f"minuto-{order.order_id}-checkout")
        url = session.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ProviderRejected("no checkout url")
        exp = pi.get("expires_at")
        expires_at = datetime.fromtimestamp(int(exp), timezone.utc) if isinstance(exp, (int, float)) else None
        return CreatedPayment(provider_ref=pi_id, payment=PaymentInfo(redirect_url=url), expires_at=expires_at)

    def register_webhook(self, url: str) -> dict:
        """POST /v1/webhook_endpoints. Буцаах secret (whsec_…) зөвхөн НЭГ удаа ирнэ."""
        return self._request("POST", "/v1/webhook_endpoints",
                             {"url": url, "enabled_events": ["payment_intent.succeeded", "payment_intent.payment_failed",
                                                             "payment_intent.canceled"]},
                             idem=f"minuto-webhook-{hashlib.sha256(url.encode()).hexdigest()[:16]}")

    def check_payment(self, provider_ref: str) -> PaymentCheck:
        pi = self._request("GET", f"/v1/payment_intents/{provider_ref}")
        raw = str(pi.get("status") or "")
        status: Status = _STATUS_MAP.get(raw, "pending" if raw in _PENDING or not raw else "failed")
        exp = pi.get("expires_at")
        if status == "pending" and isinstance(exp, (int, float)) and exp < time.time():
            status = "expired"
        meta = pi.get("metadata") if isinstance(pi.get("metadata"), dict) else {}
        amount = pi.get("amount")
        return PaymentCheck(status=status, amount_mnt=int(amount) if isinstance(amount, (int, float)) else None,
                            currency=pi.get("currency"), order_id=meta.get("order_id"))

    def parse_webhook(self, body: bytes, headers: Dict[str, str]) -> Optional[str]:
        event = verify_wire_signature(body, headers.get(SIGNATURE_HEADER.lower()) or headers.get(SIGNATURE_HEADER), self._secret)
        etype = str(event.get("type") or "")
        if not etype.startswith("payment_intent.") and not etype.startswith("charge."):
            return None
        obj = (event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None
        if not isinstance(obj, dict):
            return None
        if etype.startswith("charge."):
            ref = obj.get("payment_intent")
        else:
            ref = obj.get("id")
        return ref if isinstance(ref, str) and ref else None


# ---------------------------------------------------------------- Fake (тест, локал хөгжүүлэлт)

class FakeProvider:
    """Сүлжээгүй provider: тест дотор `fake.mark(ref, "paid")` гэж төлбөрийг дуурайна."""

    name = "fake"

    def __init__(self) -> None:
        self.intents: Dict[str, dict] = {}
        self.calls: List[str] = []
        self.fail_create: Optional[Exception] = None
        self._lock = threading.Lock()

    def create_payment(self, order: OrderRef) -> CreatedPayment:
        self.calls.append("create")
        if self.fail_create:
            raise self.fail_create
        with self._lock:
            ref = f"pi_fake_{order.order_id[:8]}"
            self.intents.setdefault(ref, {"status": "pending", "amount": order.amount_mnt, "order_id": order.order_id})
        return CreatedPayment(ref, PaymentInfo(redirect_url=f"https://pay.example/{ref}", qr_text=f"QR:{ref}"))

    def mark(self, ref: str, status: Status, amount: Optional[int] = None) -> None:
        self.intents[ref]["status"] = status
        if amount is not None:
            self.intents[ref]["amount"] = amount

    def check_payment(self, provider_ref: str) -> PaymentCheck:
        self.calls.append("check")
        it = self.intents.get(provider_ref)
        if not it:
            raise ProviderRejected("unknown")
        return PaymentCheck(it["status"], it["amount"], "MNT", it["order_id"])

    def parse_webhook(self, body: bytes, headers: Dict[str, str]) -> Optional[str]:
        data = json.loads(body or b"{}")
        return data.get("ref")


# ---------------------------------------------------------------- Сонголт

_provider: Optional[PaymentProvider] = None
_provider_lock = threading.Lock()


def get_provider() -> Optional[PaymentProvider]:
    """Тохиргоогүй бол None (checkout 503/403 болно)."""
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is None and settings.wire_api_key:
            _provider = WireProvider(settings.wire_api_key, settings.wire_webhook_secret, settings.wire_base_url)
    return _provider


def set_provider(provider: Optional[PaymentProvider]) -> None:
    global _provider
    _provider = provider
