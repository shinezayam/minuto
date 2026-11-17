"""Billing: минут тоолох (shadow / enforce), багц (subscription), төлбөрийн захиалга. BILLING_PLAN.md, BILLING_STEP1_PROPOSAL.md.

Батлагдсан дүрэм:
  * Нэгж = боловсруулсан аудионы урт, хурал бүрээр минут руу ДЭЭШ бүхэлчилнэ (min 1). Free = нэг удаагийн 30 минут.
  * Боловсруулалт эхлэхэд reserve, бичвэр хадгалагдвал debit (хураангуй унасан ч), бичвэргүй дуусвал release.
  * Ижил аудиог дахин боловсруулах үнэгүй (meeting + SHA-256 fingerprint).
  * Минут дууссан: шинэ upload / шинэ бичлэг эхлэхгүй. Дууссан ШУУД бичлэгийг боловсруулахыг хэзээ ч хаахгүй
    (нэг удаа хасах руу орж болно); upload нь үлдэгдлээс урт бол татгалзана (файл хэрэглэгчид байгаа).
  * Team идэвхтэй workspace-ийн хурал → workspace-ийн минут; бусад үед хурлын ЭЗНИЙ хувийн минут.
  * Хэрэглээгүй минут дараагийн хугацаанд шилжихгүй.
Аюулгүй байдал: shadow горимд billing-ийн аливаа алдаа боловсруулалтад нөлөөлөхгүй (бүгд log + swallow).
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import (
    ContactRequest, Meeting, PaymentOrder, Plan, ProcessingJob, Subscription, UsageEvent, Workspace, WorkspaceMember,
    session_scope,
)

logger = logging.getLogger(__name__)

FREE_PLAN = "free"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: Optional[datetime]) -> Optional[datetime]:
    """SQLite timezone-гүй буцаадаг тул UTC гэж үзнэ."""
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def mode() -> str:
    return settings.billing_metering


def enabled() -> bool:
    return mode() in ("shadow", "enforce")


def enforcing() -> bool:
    return mode() == "enforce"


class BillingBlocked(Exception):
    """enforce горимд: минут хүрэлцэхгүй (402)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.message}"


class BillingError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.message}"


# ---------------------------------------------------------------- Түгжээ (нэг scope-ийн зэрэг reserve)

_sqlite_lock = threading.RLock()


@contextmanager
def _locked_session(key: str) -> Iterator[Session]:
    """Нэг scope (хэрэглэгч/workspace)-ийн reserve, төлбөр хэрэглэх үйлдлийг дараалуулна.
    Postgres: transaction-ий advisory lock (олон instance-д ч ажиллана). SQLite: процесс доторх lock (нэг instance)."""
    with session_scope() as s:
        if s.get_bind().dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"billing:{key}"})
            yield s
        else:
            with _sqlite_lock:
                yield s


# ---------------------------------------------------------------- Багц, scope, үлдэгдэл

def list_plans() -> List[dict]:
    with session_scope() as s:
        rows = s.scalars(select(Plan).where(Plan.active.is_(True)).order_by(Plan.sort)).all()
        return [plan_dict(p) for p in rows]


def plan_dict(p: Plan) -> dict:
    return {"code": p.code, "name": p.name, "kind": p.kind, "minutes": p.minutes, "period_days": p.period_days,
            "price_mnt": p.price_mnt, "scope": p.scope}


def _owner_filter(scope_type: str, scope_id: str):
    return Subscription.user_id == scope_id if scope_type == "user" else Subscription.workspace_id == scope_id


def _roll_periods(s: Session, scope_type: str, scope_id: str, now: datetime) -> Optional[Subscription]:
    """Дууссан хугацааг expired болгож, эхлэх цаг нь болсон scheduled хугацааг идэвхжүүлнэ. Идэвхтэйг буцаана."""
    active = s.scalar(select(Subscription).where(_owner_filter(scope_type, scope_id), Subscription.status == "active"))
    if active is not None and _aware(active.period_end) <= now:
        active.status = "expired"
        active.updated_at = now
        s.flush()
        active = None
    if active is None:
        nxt = s.scalars(
            select(Subscription)
            .where(_owner_filter(scope_type, scope_id), Subscription.status == "scheduled")
            .order_by(Subscription.period_start)
        ).all()
        for sub in nxt:
            if _aware(sub.period_end) <= now:
                sub.status = "expired"
                sub.updated_at = now
            elif _aware(sub.period_start) <= now:
                sub.status = "active"
                sub.updated_at = now
                active = sub
                break
        s.flush()
    return active


def _team_subscription(s: Session, workspace_id: Optional[str], now: datetime) -> Optional[Subscription]:
    """Workspace-ийн идэвхтэй багц (Team эсвэл гэрээт Enterprise)."""
    if not workspace_id:
        return None
    return _roll_periods(s, "workspace", workspace_id, now)


def scope_for_meeting(s: Session, meeting: Meeting, now: datetime) -> Tuple[str, str]:
    """Team (эсвэл workspace-ийн бусад идэвхтэй багц) байвал workspace, үгүй бол хурлын эзэн."""
    if meeting.workspace_id and _team_subscription(s, meeting.workspace_id, now) is not None:
        return "workspace", meeting.workspace_id
    return "user", meeting.owner_id


@dataclass
class Balance:
    scope_type: str
    scope_id: str
    plan: Plan
    subscription: Optional[Subscription]
    allowance: int
    used: int

    @property
    def remaining(self) -> int:
        return self.allowance - self.used


def _used(s: Session, scope_type: str, scope_id: str, subscription_id: Optional[str]) -> int:
    q = select(func.coalesce(func.sum(UsageEvent.billable_minutes), 0)).where(
        UsageEvent.scope_type == scope_type, UsageEvent.scope_id == scope_id, UsageEvent.state.in_(("reserved", "debited")),
    )
    if subscription_id:
        q = q.where(UsageEvent.subscription_id == subscription_id)
    else:
        q = q.where(UsageEvent.plan_code == FREE_PLAN)
    return int(s.scalar(q) or 0)


def balance(s: Session, scope_type: str, scope_id: str, now: Optional[datetime] = None) -> Balance:
    now = now or _now()
    sub = _roll_periods(s, scope_type, scope_id, now)
    if sub is not None:
        plan = s.get(Plan, sub.plan_code)
        return Balance(scope_type, scope_id, plan, sub, int(plan.minutes or 0), _used(s, scope_type, scope_id, sub.id))
    free = s.get(Plan, FREE_PLAN)
    if scope_type == "workspace":  # багцгүй workspace-д өөрийн минут байхгүй
        return Balance(scope_type, scope_id, free, None, 0, 0)
    return Balance(scope_type, scope_id, free, None, int(free.minutes or 0), _used(s, scope_type, scope_id, None))


def _sub_dict(sub: Optional[Subscription]) -> Optional[dict]:
    if sub is None:
        return None
    return {"id": sub.id, "plan_code": sub.plan_code, "status": sub.status,
            "period_start": _aware(sub.period_start).isoformat(), "period_end": _aware(sub.period_end).isoformat(),
            "cancel_at_period_end": bool(sub.cancel_at_period_end)}


def summary(user_id: str, workspace_id: Optional[str] = None) -> dict:
    with session_scope() as s:
        if workspace_id:
            if _role(s, user_id, workspace_id) is None:
                raise BillingError("NOT_FOUND", 404, "Workspace олдсонгүй")
            b = balance(s, "workspace", workspace_id)
        else:
            b = balance(s, "user", user_id)
        return {
            "metering": mode(),
            "checkout_enabled": checkout_enabled(),
            "scope": {"type": b.scope_type, "id": b.scope_id},
            "plan": plan_dict(b.plan),
            "subscription": _sub_dict(b.subscription),
            "minutes": {"allowance": b.allowance, "used": b.used, "remaining": b.remaining},
        }


# ---------------------------------------------------------------- Аудио: урт, fingerprint

def billable_minutes(seconds: Optional[float]) -> int:
    if seconds is None or seconds <= 0:
        return 0
    return max(1, math.ceil(round(seconds, 3) / 60))


def fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _audio_facts(meeting_id: str) -> Optional[dict]:
    from app.services import processing, upload_guard
    from app.services.audio_utils import find_meeting_audio

    d = processing.meeting_dir(meeting_id)
    audio = find_meeting_audio(d)
    if audio is None:
        return None
    source = "upload" if any(p.name.startswith("uploaded.") for p in d.iterdir()) else "live"
    seconds = upload_guard.probe_duration_sec(audio)
    if seconds is None:  # шууд бичлэгийн webm-д урт бичигддэггүй: decode хийж хэмжинэ
        from app.services.audio_utils import decoded_duration_sec

        seconds = decoded_duration_sec(audio)
    return {"source": source, "seconds": seconds, "fingerprint": fingerprint(audio)}


# ---------------------------------------------------------------- Reserve / settle / reconcile

def _minutes_text(n: int) -> str:
    return f"{n} минут"


def reserve(meeting_id: str, actor_user_id: Optional[str]) -> Optional[str]:
    """POST /process дээр (try_begin_processing-ээс ӨМНӨ) дуудна. off бол юу ч хийхгүй.
    enforce горимд хүрэлцэхгүй бол BillingBlocked. shadow горимд бүх алдааг залгина. Буцаах: usage event id."""
    if not enabled():
        return None
    if not enforcing():
        try:
            return _reserve(meeting_id, actor_user_id, shadow=True)
        except Exception:  # noqa: BLE001 — shadow хэзээ ч боловсруулалтад саад болохгүй
            logger.exception("billing shadow reserve failed for meeting %s", meeting_id)
            return None
    return _reserve(meeting_id, actor_user_id, shadow=False)


def _reserve(meeting_id: str, actor_user_id: Optional[str], shadow: bool) -> Optional[str]:
    facts = _audio_facts(meeting_id)
    if facts is None:
        return None
    seconds = facts["seconds"]
    minutes = billable_minutes(seconds)
    now = _now()
    with session_scope() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            return None
        scope_type, scope_id = scope_for_meeting(s, meeting, now)
    with _locked_session(f"{scope_type}:{scope_id}") as s:
        existing = s.scalar(select(UsageEvent).where(
            UsageEvent.meeting_id == meeting_id, UsageEvent.audio_fingerprint == facts["fingerprint"],
            UsageEvent.state.in_(("reserved", "debited")),
        ))
        if existing is not None:
            if not (existing.duration_source == "unknown" and seconds is not None):
                return existing.id  # ижил аудиог дахин хасахгүй (давхар хүсэлт, дахин боловсруулалт)
            # Өмнө урт нь тодорхойгүй (0 минут) бүртгэгдсэн: тэрийг чөлөөлж, одоо хэмжсэн уртаар шинээр тооцно
            existing.state = "released"
            existing.settled_at = now
            existing.reason = (existing.reason + "; " if existing.reason else "") + "re-measured"
            s.flush()
        b = balance(s, scope_type, scope_id, now)
        remaining = b.remaining
        if seconds is None:
            decision, reason = "allowed", "duration unknown: not charged"
        elif minutes <= remaining:
            decision, reason = "allowed", None
        elif facts["source"] == "live":
            # Дахин бичиж болохгүй бичлэгийг хэзээ ч түгжихгүй — нэг удаа хасах руу орно
            decision, reason = ("would_overdraft" if shadow else "overdraft"), f"needs {minutes}, has {remaining}"
        else:
            decision, reason = "would_block", f"needs {minutes}, has {remaining}"
            if not shadow:
                if remaining <= 0:
                    raise BillingBlocked("MINUTES_EXHAUSTED", "Таны минут дууссан байна. Багцаа сунгах эсвэл ахиулна уу.")
                raise BillingBlocked(
                    "MINUTES_INSUFFICIENT",
                    f"Энэ файлд {_minutes_text(minutes)} хэрэгтэй, {_minutes_text(max(remaining, 0))} үлдсэн байна.",
                )
        ev = UsageEvent(
            scope_type=scope_type, scope_id=scope_id, subscription_id=b.subscription.id if b.subscription else None,
            meeting_id=meeting_id, actor_user_id=actor_user_id,
            plan_code=b.plan.code, source=facts["source"],
            audio_seconds=int(round(seconds)) if seconds is not None else None,
            duration_source="probe" if seconds is not None else "unknown",
            billable_minutes=minutes if seconds is not None else 0,
            audio_fingerprint=facts["fingerprint"], state="reserved", shadow=shadow,
            decision=decision, remaining_before=remaining, reason=reason, created_at=now,
        )
        s.add(ev)
        try:
            s.flush()
        except IntegrityError:  # өөр хүсэлт яг одоо хадгалсан (олон instance) — тэрийг ашиглана
            s.rollback()
            return None
        return ev.id


def _transcript_saved(s: Session, meeting_id: str) -> bool:
    """ЭНЭ удаагийн боловсруулалт бичвэр хүргэсэн эсэх. (Хуучин бичвэр байгаа эсэхээр биш — дахин боловсруулалт
    амжилтгүй болбол хуучин бичвэр үлдсэн ч минут хасахгүй.) Амжилттай = алдаагүй дууссан, эсвэл зөвхөн хураангуй унасан."""
    m = s.get(Meeting, meeting_id)
    if m is None or not m.transcript or m.status == "processing":
        return False
    err = m.processing_error or ""
    return m.status == "completed" and not err or err.startswith("SUMMARY_FAILED")


def settle(meeting_id: str) -> int:
    """Ажил эцэслэн дуусахад (амжилттай/алдаатай). Бичвэр хадгалагдсан бол debit, үгүй бол release. Хэдийг өөрчилснийг буцаана."""
    if not enabled():
        return 0
    try:
        with session_scope() as s:
            rows = s.scalars(select(UsageEvent).where(UsageEvent.meeting_id == meeting_id, UsageEvent.state == "reserved")).all()
            if not rows:
                return 0
            new_state = "debited" if _transcript_saved(s, meeting_id) else "released"
            now = _now()
            for ev in rows:
                ev.state = new_state
                ev.settled_at = now
            return len(rows)
    except Exception:  # noqa: BLE001
        logger.exception("billing settle failed for meeting %s", meeting_id)
        return 0


def release(event_id: Optional[str]) -> None:
    """Reserve хийсний дараа ажил эхэлж чадаагүй (409 гэх мэт) үед."""
    if not event_id or not enabled():
        return
    try:
        with session_scope() as s:
            s.execute(update(UsageEvent).where(UsageEvent.id == event_id, UsageEvent.state == "reserved")
                      .values(state="released", settled_at=_now(), reason="not started"))
    except Exception:  # noqa: BLE001
        logger.exception("billing release failed for %s", event_id)


def reconcile(now: Optional[datetime] = None) -> int:
    """Өнчин reserved мөрүүд (идэвхтэй ажилгүй, хуучин): бичвэр байвал debit, үгүй (эсвэл хурал устсан) бол release."""
    if not enabled():
        return 0
    now = now or _now()
    cutoff = now - timedelta(seconds=settings.job_timeout_sec + settings.job_lease_sec)
    n = 0
    try:
        with session_scope() as s:
            rows = s.scalars(select(UsageEvent).where(UsageEvent.state == "reserved", UsageEvent.created_at < cutoff)).all()
            for ev in rows:
                active = s.scalar(select(ProcessingJob.id).where(
                    ProcessingJob.meeting_id == ev.meeting_id, ProcessingJob.state.in_(("queued", "running"))))
                if active:
                    continue
                ev.state = "debited" if ev.meeting_id and _transcript_saved(s, ev.meeting_id) else "released"
                ev.settled_at = now
                ev.reason = (ev.reason + "; " if ev.reason else "") + "reconciled"
                n += 1
        if n:
            logger.warning("billing reconcile settled %d orphan reservation(s)", n)
    except Exception:  # noqa: BLE001
        logger.exception("billing reconcile failed")
    return n


def check_can_start_recording(meeting_id: str, user_id: str) -> None:
    """Шинэ шууд бичлэг эхлэхэд (WS-ийн анхны холболт). enforce горимд үлдэгдэл 0 бол BillingBlocked.
    Эхэлсэн бичлэгийг (хэсэг ирсэн) хэзээ ч зогсоохгүй — дуудагч тал үүнийг шалгана."""
    if not enforcing():
        return
    with session_scope() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            return
        now = _now()
        scope_type, scope_id = scope_for_meeting(s, meeting, now)
        if balance(s, scope_type, scope_id, now).remaining <= 0:
            raise BillingBlocked("MINUTES_EXHAUSTED", "Таны минут дууссан байна. Багцаа сунгах эсвэл ахиулна уу.")


# ---------------------------------------------------------------- Захиалга, төлбөр

def checkout_enabled() -> bool:
    from app.services import payments

    return bool(settings.billing_checkout_enabled and payments.get_provider() is not None)


def _role(s: Session, user_id: str, workspace_id: str) -> Optional[str]:
    ws = s.get(Workspace, workspace_id)
    if ws is None:
        return None
    if ws.owner_id == user_id:
        return "owner"
    return s.scalar(select(WorkspaceMember.role).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id))


def order_dict(o: PaymentOrder) -> dict:
    return {
        "id": o.id, "plan_code": o.plan_code, "amount_mnt": o.amount_mnt, "status": o.status,
        "workspace_id": o.workspace_id, "payment": o.payment if o.status == "pending" else None,
        "created_at": _aware(o.created_at).isoformat(), "paid_at": _aware(o.paid_at).isoformat() if o.paid_at else None,
    }


def create_checkout(user_id: str, plan_code: str, workspace_id: Optional[str]) -> dict:
    from app.services import payments

    provider = payments.get_provider()
    if not settings.billing_checkout_enabled or provider is None:
        raise BillingError("BILLING_DISABLED", 403, "Төлбөр төлөх боломж одоогоор нээгдээгүй байна")
    with session_scope() as s:
        plan = s.get(Plan, plan_code)
        if plan is None or not plan.active or plan.kind != "paid" or not plan.price_mnt:
            raise BillingError("INVALID_PLAN", 400, "Ийм багц байхгүй")
        if plan.scope == "workspace":
            if not workspace_id:
                raise BillingError("WORKSPACE_REQUIRED", 422, "Team багцыг аль workspace-д авахаа сонгоно уу")
            if _role(s, user_id, workspace_id) not in ("owner", "admin"):
                raise BillingError("FORBIDDEN", 403, "Зөвхөн workspace-ийн эзэн эсвэл админ багц авна")
        else:
            workspace_id = None
        order = PaymentOrder(user_id=user_id, workspace_id=workspace_id, plan_code=plan.code, amount_mnt=int(plan.price_mnt),
                             status="pending", provider=provider.name, created_at=_now(), updated_at=_now())
        s.add(order)
        s.flush()
        order_id, amount, plan_name, period = order.id, order.amount_mnt, plan.name, plan.period_days
    ref = payments.OrderRef(
        order_id=order_id, amount_mnt=amount, description=f"Minuto {plan_name}, {period} хоног",
        return_url=f"{settings.public_frontend_url}/billing/return?order={order_id}",
        metadata={"plan_code": plan_code},
    )
    try:
        created = provider.create_payment(ref)
    except (payments.ProviderUnavailable, payments.ProviderRejected) as e:
        logger.warning("checkout: provider create failed for order %s: %s", order_id, e)
        with session_scope() as s:
            s.execute(update(PaymentOrder).where(PaymentOrder.id == order_id)
                      .values(status="failed", last_error=str(e)[:200], updated_at=_now()))
        raise BillingError("PAYMENT_UNAVAILABLE", 503, "Төлбөрийн систем түр ажиллахгүй байна. Дахин оролдоно уу.") from e
    with session_scope() as s:
        o = s.get(PaymentOrder, order_id)
        o.provider_ref = created.provider_ref
        o.payment = created.payment.as_dict()
        o.updated_at = _now()
        s.flush()
        return order_dict(o)


def get_order(user_id: str, order_id: str, refresh: bool = True) -> dict:
    with session_scope() as s:
        o = s.get(PaymentOrder, order_id)
        if o is None or o.user_id != user_id:
            raise BillingError("NOT_FOUND", 404, "Захиалга олдсонгүй")
        pending = o.status == "pending" and o.provider_ref
    if refresh and pending:
        refresh_order(order_id)
    with session_scope() as s:
        return order_dict(s.get(PaymentOrder, order_id))


def list_orders(user_id: str) -> List[dict]:
    with session_scope() as s:
        rows = s.scalars(select(PaymentOrder).where(PaymentOrder.user_id == user_id)
                         .order_by(PaymentOrder.created_at.desc()).limit(50)).all()
        return [order_dict(o) for o in rows]


def refresh_order(order_id: str) -> Optional[str]:
    """Provider-ээс сервер талаас шалгана. Төлөгдсөн, дүн таарсан, захиалгын id таарсан үед л багц идэвхжинэ."""
    from app.services import payments

    provider = payments.get_provider()
    with session_scope() as s:
        o = s.get(PaymentOrder, order_id)
        if o is None or o.status != "pending" or not o.provider_ref or provider is None or provider.name != o.provider:
            return o.status if o else None
        ref, amount = o.provider_ref, o.amount_mnt
    try:
        check = provider.check_payment(ref)
    except (payments.ProviderUnavailable, payments.ProviderRejected) as e:
        logger.warning("order %s: payment check failed: %s", order_id, e)
        return "pending"
    if check.status == "paid":
        if check.amount_mnt != amount or (check.currency and check.currency.upper() != "MNT") or (check.order_id and check.order_id != order_id):
            logger.error("order %s: paid intent does not match order (amount/currency/order id) — NOT applied", order_id)
            with session_scope() as s:
                s.execute(update(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.status == "pending")
                          .values(last_error="MISMATCH: paid amount/currency/order differs", updated_at=_now()))
            return "pending"
        apply_paid_order(order_id)
        return "paid"
    if check.status in ("failed", "expired", "cancelled"):
        with session_scope() as s:
            s.execute(update(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.status == "pending")
                      .values(status=check.status, updated_at=_now()))
        return check.status
    return "pending"


def apply_paid_order(order_id: str, now: Optional[datetime] = None) -> bool:
    """Нэг transaction, idempotent: давхар webhook/poll хоёр дахин багц өгөхгүй. Шинээр хэрэгжсэн бол True."""
    now = now or _now()
    with session_scope() as s:
        o = s.get(PaymentOrder, order_id)
        if o is None:
            return False
        scope_type, scope_id = ("workspace", o.workspace_id) if o.workspace_id else ("user", o.user_id)
    if not scope_id:
        return False
    with _locked_session(f"{scope_type}:{scope_id}") as s:
        o = s.get(PaymentOrder, order_id)
        if o.status == "paid":
            return False
        plan = s.get(Plan, o.plan_code)
        days = int(plan.period_days or 30)
        active = _roll_periods(s, scope_type, scope_id, now)
        owner = {"user_id": scope_id} if scope_type == "user" else {"workspace_id": scope_id}
        if active is None:
            status, start = "active", now
        elif active.plan_code == plan.code or (plan.minutes or 0) < (s.get(Plan, active.plan_code).minutes or 0):
            # Сунгалт эсвэл бууралт: одоогийн хугацаа (эсвэл хамгийн сүүлийн scheduled) дууссаны дараа эхэлнэ
            last_end = s.scalar(select(func.max(Subscription.period_end)).where(
                _owner_filter(scope_type, scope_id), Subscription.status.in_(("active", "scheduled"))))
            status, start = "scheduled", max(_aware(last_end), now)
            active.cancel_at_period_end = False
        else:
            # Ахиулалт: шууд хүчинтэй. (Үнийн зөрүүний дүрэм батлагдаагүй — одоогоор тооцохгүй.)
            active.status = "expired"
            active.period_end = now
            active.updated_at = now
            s.flush()
            status, start = "active", now
        s.add(Subscription(plan_code=plan.code, status=status, period_start=start, period_end=start + timedelta(days=days),
                           order_id=o.id, created_at=now, updated_at=now, **owner))
        o.status = "paid"
        o.paid_at = now
        o.updated_at = now
        o.payment = None
        logger.info("order %s paid: %s %s for %s:%s", o.id, plan.code, status, scope_type, scope_id)
        return True


def cancel(user_id: str, workspace_id: Optional[str]) -> dict:
    with session_scope() as s:
        if workspace_id:
            if _role(s, user_id, workspace_id) not in ("owner", "admin"):
                raise BillingError("FORBIDDEN", 403, "Зөвхөн workspace-ийн эзэн эсвэл админ цуцална")
            scope_type, scope_id = "workspace", workspace_id
        else:
            scope_type, scope_id = "user", user_id
        sub = _roll_periods(s, scope_type, scope_id, _now())
        if sub is None:
            raise BillingError("NO_SUBSCRIPTION", 404, "Идэвхтэй багц алга")
        sub.cancel_at_period_end = True
        sub.updated_at = _now()
        s.flush()
        return _sub_dict(sub)


def handle_webhook(body: bytes, headers: dict) -> Optional[str]:
    """Гарын үсгийг шалгаад, аль intent болохыг олж, СЕРВЕР талаас дахин шалгана. Буцаах: захиалгын шинэ төлөв."""
    from app.services import payments

    provider = payments.get_provider()
    if provider is None:
        raise BillingError("BILLING_DISABLED", 503, "Төлбөр тохируулаагүй")
    ref = provider.parse_webhook(body, headers)  # InvalidSignature → 400
    if not ref:
        return None
    with session_scope() as s:
        order_id = s.scalar(select(PaymentOrder.id).where(PaymentOrder.provider == provider.name, PaymentOrder.provider_ref == ref))
    if order_id is None:
        logger.warning("webhook for unknown payment intent ignored")
        return None
    return refresh_order(order_id)


def refresh_pending_orders(limit: int = 50) -> int:
    """Webhook алдагдсан тохиолдолд: сүүлийн 2 цагийн pending захиалгуудыг шалгана."""
    if not checkout_enabled():
        return 0
    since = _now() - timedelta(hours=2)
    with session_scope() as s:
        ids = s.scalars(select(PaymentOrder.id).where(PaymentOrder.status == "pending", PaymentOrder.created_at >= since)
                        .order_by(PaymentOrder.created_at).limit(limit)).all()
    n = 0
    for oid in ids:
        try:
            if refresh_order(oid) == "paid":
                n += 1
        except Exception:  # noqa: BLE001
            logger.exception("refresh of order %s failed", oid)
    return n


# ---------------------------------------------------------------- Enterprise хүсэлт

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def create_contact_request(name: str, email: str, message: str, company: Optional[str], phone: Optional[str],
                           user_id: Optional[str]) -> str:
    name, email, message = name.strip(), email.strip(), message.strip()
    if not name or not message:
        raise BillingError("INVALID_INPUT", 422, "Нэр болон мессежээ бөглөнө үү")
    if not _EMAIL.match(email):
        raise BillingError("INVALID_EMAIL", 422, "И-мэйл хаяг буруу байна")
    with session_scope() as s:
        req = ContactRequest(name=name[:100], email=email[:320], message=message[:5000],
                             company=(company or "").strip()[:200] or None, phone=(phone or "").strip()[:40] or None,
                             user_id=user_id, status="new", created_at=_now())
        s.add(req)
        s.flush()
        logger.info("enterprise contact request %s received", req.id)
        return req.id


FEEDBACK_DAILY_LIMIT = 20


def create_feedback(user_id: str, name: str, email: str, category: str, message: str) -> str:
    """Тохиргооноос ирсэн санал хүсэлт. Админы "Хүсэлтүүд" хуудсанд kind=feedback-ээр харагдана."""
    message = message.strip()
    if len(message) < 3:
        raise BillingError("INVALID_INPUT", 422, "Санал хүсэлтээ бичнэ үү")
    since = _now() - timedelta(days=1)
    with session_scope() as s:
        recent = s.scalar(
            select(func.count()).select_from(ContactRequest).where(
                ContactRequest.kind == "feedback", ContactRequest.user_id == user_id, ContactRequest.created_at >= since
            )
        )
        if (recent or 0) >= FEEDBACK_DAILY_LIMIT:
            raise BillingError("TOO_MANY_FEEDBACK", 429, "Өнөөдөр хэт олон санал илгээсэн байна. Маргааш дахин оролдоно уу")
        req = ContactRequest(name=(name.strip() or email)[:100], email=email[:320], message=message[:5000],
                             user_id=user_id, status="new", kind="feedback", category=category, created_at=_now())
        s.add(req)
        s.flush()
        logger.info("feedback %s (%s) received", req.id, category)
        return req.id
