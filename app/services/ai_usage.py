"""Гадны AI API-ийн хэрэглээ, зардлын бүртгэл (ai_usage хүснэгт, админы "AI зардал" хуудас).

Дуудлага бүр (Scribe-ийн нэг хүсэлт, Qwen-ий нэг хариу) нэг мөр бичнэ. Аль хурал/хэрэглэгчид хамаарахыг дуудагч тал
`scope(...)`-оор (contextvars) зааж өгнө, тиймээс provider-ийн код хурлын ID-г мэдэх шаардлагагүй.
Бүртгэлийн алдаа хэзээ ч үндсэн ажилд (бичвэр, тэмдэглэл) нөлөөлөхгүй.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from sqlalchemy import func, select

from app.config import settings
from app.db import AiUsage, Meeting, UsageEvent, User, session_scope

logger = logging.getLogger(__name__)

PURPOSES = ("asr_file", "asr_live", "llm_minutes", "llm_summary", "llm_ask", "llm_topics")


@dataclass(frozen=True)
class Scope:
    meeting_id: Optional[str] = None
    user_id: Optional[str] = None
    llm_purpose: str = "llm_minutes"


_scope: ContextVar[Scope] = ContextVar("ai_usage_scope", default=Scope())


@contextmanager
def scope(meeting_id: Optional[str] = None, user_id: Optional[str] = None,
          llm_purpose: Optional[str] = None) -> Iterator[None]:
    cur = _scope.get()
    token = _scope.set(replace(cur, meeting_id=meeting_id or cur.meeting_id, user_id=user_id or cur.user_id,
                               llm_purpose=llm_purpose or cur.llm_purpose))
    try:
        yield
    finally:
        _scope.reset(token)


def asr_cost_usd(seconds: float, keyterms: bool) -> float:
    per_hour = settings.price_scribe_usd_per_hour + (settings.price_scribe_keyterms_usd_per_hour if keyterms else 0.0)
    return max(0.0, seconds) / 3600.0 * per_hour


def llm_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (max(0, input_tokens) * settings.price_llm_input_usd_per_m
            + max(0, output_tokens) * settings.price_llm_output_usd_per_m) / 1_000_000


def _insert(row: AiUsage) -> None:
    try:
        with session_scope() as s:
            s.add(row)
    except Exception:  # noqa: BLE001 — бүртгэл унасан ч үндсэн ажил үргэлжилнэ
        logger.exception("ai_usage: insert failed (%s)", row.purpose)


async def record_asr(seconds: float, *, live: bool, keyterms: bool, provider: str, model: str) -> None:
    sc = _scope.get()
    row = AiUsage(meeting_id=sc.meeting_id, user_id=sc.user_id, purpose="asr_live" if live else "asr_file",
                  provider=provider, model=(model or "")[:60], audio_seconds=float(max(0.0, seconds)),
                  input_tokens=0, output_tokens=0, cost_usd=asr_cost_usd(seconds, keyterms))
    await asyncio.to_thread(_insert, row)


async def record_llm(usage: Optional[dict], *, provider: str, model: str, purpose: Optional[str] = None) -> None:
    usage = usage if isinstance(usage, dict) else {}
    try:
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        tin = tout = 0
    sc = _scope.get()
    row = AiUsage(meeting_id=sc.meeting_id, user_id=sc.user_id, purpose=purpose or sc.llm_purpose,
                  provider=provider, model=(model or "")[:60], audio_seconds=0.0, input_tokens=tin,
                  output_tokens=tout, cost_usd=llm_cost_usd(tin, tout))
    await asyncio.to_thread(_insert, row)


# ---------------------------------------------------------------- Админы тайлан

def _money(usd: float) -> dict:
    return {"cost_usd": round(usd, 4), "cost_mnt": round(usd * settings.usd_to_mnt)}


def report(days: int = 30, now: Optional[datetime] = None) -> dict:
    """Сүүлийн `days` хоногийн AI зардал: нийт, зориулалт, өдөр, хамгийн их зардалтай хэрэглэгчид, тооцсон минут."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    with session_scope() as s:
        rows = s.scalars(select(AiUsage).where(AiUsage.created_at >= since)).all()
        mids = {r.meeting_id for r in rows if r.meeting_id}
        owners = dict(s.execute(select(Meeting.id, Meeting.owner_id).where(Meeting.id.in_(mids))).all()) if mids else {}

        totals = {"cost_usd": 0.0, "audio_seconds": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": len(rows)}
        by_purpose: dict[str, dict] = {}
        by_day: dict[str, float] = {}
        by_user: dict[str, dict] = {}
        for r in rows:
            cost = r.cost_usd or 0.0
            totals["cost_usd"] += cost
            totals["audio_seconds"] += r.audio_seconds or 0.0
            totals["input_tokens"] += r.input_tokens or 0
            totals["output_tokens"] += r.output_tokens or 0
            p = by_purpose.setdefault(r.purpose, {"purpose": r.purpose, "cost_usd": 0.0, "requests": 0,
                                                  "audio_seconds": 0.0, "input_tokens": 0, "output_tokens": 0})
            p["cost_usd"] += cost
            p["requests"] += 1
            p["audio_seconds"] += r.audio_seconds or 0.0
            p["input_tokens"] += r.input_tokens or 0
            p["output_tokens"] += r.output_tokens or 0
            created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
            day = created.date().isoformat()
            by_day[day] = by_day.get(day, 0.0) + cost
            uid = r.user_id or owners.get(r.meeting_id or "")
            if uid:
                u = by_user.setdefault(uid, {"user_id": uid, "cost_usd": 0.0, "meetings": set()})
                u["cost_usd"] += cost
                if r.meeting_id:
                    u["meetings"].add(r.meeting_id)

        top = sorted(by_user.values(), key=lambda u: u["cost_usd"], reverse=True)[:10]
        emails = dict(s.execute(select(User.id, User.email).where(User.id.in_([u["user_id"] for u in top]))).all()) \
            if top else {}
        billed = int(s.scalar(select(func.coalesce(func.sum(UsageEvent.billable_minutes), 0)).where(
            UsageEvent.state == "debited", UsageEvent.created_at >= since)) or 0)

    return {
        "days": days,
        "usd_to_mnt": settings.usd_to_mnt,
        "prices": {
            "scribe_usd_per_hour": settings.price_scribe_usd_per_hour,
            "scribe_keyterms_usd_per_hour": settings.price_scribe_keyterms_usd_per_hour,
            "llm_input_usd_per_m": settings.price_llm_input_usd_per_m,
            "llm_output_usd_per_m": settings.price_llm_output_usd_per_m,
        },
        "totals": {"requests": totals["requests"], "audio_seconds": round(totals["audio_seconds"], 1),
                   "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
                   **_money(totals["cost_usd"])},
        "billed_minutes": billed,
        "cost_per_billed_minute_mnt": round(totals["cost_usd"] * settings.usd_to_mnt / billed, 1) if billed else None,
        "by_purpose": [{**p, "audio_seconds": round(p["audio_seconds"], 1), **_money(p["cost_usd"])}
                       for p in sorted(by_purpose.values(), key=lambda p: p["cost_usd"], reverse=True)],
        "by_day": [{"date": d, **_money(by_day.get(d, 0.0))}
                   for d in ((now - timedelta(days=i)).date().isoformat() for i in range(days - 1, -1, -1))],
        "top_users": [{"user_id": u["user_id"], "email": emails.get(u["user_id"]), "meetings": len(u["meetings"]),
                       **_money(u["cost_usd"])} for u in top],
    }
