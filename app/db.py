"""Мэдээллийн сан: DATABASE_URL (Postgres) байвал түүнийг, үгүй бол локал SQLite файлыг ашиглана."""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy import false as sa_false, true as sa_true
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Хурлын тэмдэглэлд "Хөтөлсөн: Minuto / <нэр>" гэж гарна
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Нэмэгдүүлснээр хуучин токенууд хүчингүй болно (revision 0002)
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # "Google-ээр нэвтрэх": Google-ийн тогтмол хэрэглэгчийн ID (sub). NULL = Google-ээр холбоогүй (revision 0007)
    google_sub: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("uq_users_google_sub", "google_sub", unique=True),)


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    language: Mapped[str] = mapped_column(String(5), default="mn")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    pause_accumulated_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    last_resume_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Ард боловсруулах явц
    processing_stage: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    processing_done: Mapped[int] = mapped_column(Integer, default=0)
    processing_total: Mapped[int] = mapped_column(Integer, default=0)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    transcript: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    minutes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Хурлын оролцогчдын нэрс (хэрэглэгч оруулсан)
    participants: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Архивласан эсэх (soft delete)
    archived: Mapped[bool] = mapped_column(Integer, default=0)
    # "Анхаарах" (яаралтай) гэж тэмдэглэсэн хугацаа; NULL = тэмдэглээгүй (revision 0006)
    pinned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Ашигласан загвар
    template_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # Workspace болон Project холбоос
    workspace_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)


class Contact(Base):
    """Хэрэглэгчийн өмнө нь оруулсан оролцогчид — дараагийн хурал дээр дахин бичихгүй сонгоно."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_contact_owner_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VocabularyTerm(Base):
    """Хэрэглэгчийн толь бичиг: ASR-д зөв бичихэд туслах үг, мөн (wrong байвал) автомат засвар."""

    __tablename__ = "vocabulary_terms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    term: Mapped[str] = mapped_column(String(200))
    # Буруу танигддаг хэлбэр. Байвал шинэ бичвэрт автоматаар term болгож засна.
    wrong: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeetingTemplate(Base):
    """Хурлын загвар - урьдчилан тодорхойлсон бүтэц, prompt."""

    __tablename__ = "meeting_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    # Хэрэглэгчийн загвар бол owner_id байна. Default загвар бол NULL (system-level).
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    icon: Mapped[str] = mapped_column(String(10), default="📋")  # emoji
    # Аль хэсгүүдийг үүсгэх: ["summary", "topics", "decisions", "action_items", "open_questions"]
    sections: Mapped[list] = mapped_column(JSON, default=lambda: ["summary", "topics", "decisions", "action_items"])
    # LLM-д өгөх нэмэлт заавар
    custom_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Default загвар эсэх
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TranscriptRevision(Base):
    """Бичвэрийн өөрчлөлтийн түүх (append-only). Өөрчлөлт хийхээс ӨМНӨХ бичвэрийн хуулбарыг хадгална → сэргээх боломжтой."""

    __tablename__ = "transcript_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    # Хэн өөрчилсөн (боловсруулалт, систем бол NULL)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # edit | replace | restore | reprocess
    kind: Mapped[str] = mapped_column(String(20))
    # Өөрчлөгдсөн сегментийн id-нууд
    changed: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    transcript: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ProcessingJob(Base):
    """Боловсруулалтын ажил (аудио → бичвэр → хурлын бичиг). Сервер унасан ч DB-д үлдэж, дахин эхлэхэд үргэлжилнэ."""

    __tablename__ = "processing_jobs"
    __table_args__ = (
        # Нэг хуралд зэрэг ганц идэвхтэй ажил: давхар POST /process нэг ажил үүсгэнэ
        Index(
            "uq_processing_jobs_active_meeting", "meeting_id", unique=True,
            sqlite_where=text("state IN ('queued','running')"), postgresql_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    # queued | running | succeeded | failed
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)  # retry backoff
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Дууссан ASR хэсгүүд {"<index>": {"text","start_ms","end_ms"}} — үргэлжлүүлэхэд дахин хийхгүй
    chunks: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Workspace(Base):
    """Workspace - хэрэглэгчийн хувийн эсвэл багийн ажлын орон зай."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # personal эсвэл organization
    workspace_type: Mapped[str] = mapped_column(String(20), default="personal")
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class WorkspaceMember(Base):
    """Workspace-ийн гишүүд - role-based access."""

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # owner, admin, editor, viewer
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Project(Base):
    """Project/Folder - хурлуудыг бүлэглэх."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(7), default="#5965D8")  # hex color
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------- Billing (revision 0005)

_LIVE_USAGE = text("state IN ('reserved','debited')")
_ACTIVE_SUB = text("status = 'active'")


class Plan(Base):
    """Багц: үнэ, минут нь зөвхөн энд (migration-оор seed) — клиентээс ирсэн үнэд хэзээ ч итгэхгүй."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(10))  # free | paid | custom
    minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    period_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    price_mnt: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(10))  # user | workspace
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_true())


class Subscription(Base):
    """Нэг төлбөрт хугацаа (30 хоног). status: active | scheduled (дараагийн хугацаа) | expired."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("(user_id IS NULL) <> (workspace_id IS NULL)", name="ck_subscription_one_owner"),
        Index("uq_subscriptions_active_user", "user_id", unique=True, sqlite_where=_ACTIVE_SUB, postgresql_where=_ACTIVE_SUB),
        Index("uq_subscriptions_active_workspace", "workspace_id", unique=True, sqlite_where=_ACTIVE_SUB, postgresql_where=_ACTIVE_SUB),
        Index("uq_subscriptions_order", "order_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    status: Mapped[str] = mapped_column(String(10))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    order_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageEvent(Base):
    """Минутын ledger (append-only төлөвтэй): reserved → debited | released. FK-гүй: түүх устахгүй."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_scope", "scope_type", "scope_id", "created_at"),
        # Ижил аудиог хоёр дахин хасахгүй (давхар /process, retry, дахин боловсруулалт)
        Index("uq_usage_events_audio", "meeting_id", "audio_fingerprint", unique=True, sqlite_where=_LIVE_USAGE, postgresql_where=_LIVE_USAGE),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_type: Mapped[str] = mapped_column(String(10))  # user | workspace
    scope_id: Mapped[str] = mapped_column(String(36))
    subscription_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    meeting_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    plan_code: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(10))  # live | upload
    audio_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_source: Mapped[str] = mapped_column(String(10))  # probe | unknown
    billable_minutes: Mapped[int] = mapped_column(Integer)
    audio_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(10), index=True)  # reserved | debited | released
    shadow: Mapped[bool] = mapped_column(Boolean)
    decision: Mapped[str] = mapped_column(String(20))  # allowed | would_block | overdraft | would_overdraft
    remaining_before: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentOrder(Base):
    """Төлбөрийн захиалга. paid нь эцсийн төлөв, өөрчлөгдөхгүй. Дүнг зөвхөн plans хүснэгтээс авна."""

    __tablename__ = "payment_orders"
    __table_args__ = (Index("uq_payment_orders_provider_ref", "provider", "provider_ref", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    amount_mnt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), index=True)  # pending | paid | expired | failed | cancelled
    provider: Mapped[str] = mapped_column(String(20))
    provider_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    payment: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # QR / redirect мэдээлэл (pending үед)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ContactRequest(Base):
    """Enterprise үнийн санал авах хүсэлт."""

    __tablename__ = "contact_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(320))
    company: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class AudioPurge(Base):
    """Remote (Supabase)-аас устгаж чадаагүй хурлын аудио: дахин оролдоно (revision 0008). FK-гүй: хурал аль хэдийн устсан."""

    __tablename__ = "audio_purges"

    meeting_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def dialect() -> str:
    return _engine.dialect.name if _engine is not None else ""


def normalize_url(url: str) -> str:
    """Render/Heroku-ийн postgres:// хаягийг SQLAlchemy + psycopg3 хэлбэрт оруулна."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def configure(url: Optional[str] = None) -> Engine:
    """Engine үүсгэж, хүснэгтүүдийг (байхгүй бол) үүсгэнэ. Тест бүр өөрийн SQLite-тэй дуудаж болно."""
    global _engine, _SessionLocal
    url = normalize_url(url or settings.database_url or "sqlite:///./minuto.db")
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(_engine, expire_on_commit=False)
    # Схемийг зөвхөн Alembic удирдана (app/migrations). Шинэ DB-д baseline үүсгэнэ, байгаа DB-д юу ч эвдэхгүй.
    from app.migrate import upgrade_to_head

    upgrade_to_head(_engine)
    logger.info("Database ready: %s", _engine.url.render_as_string(hide_password=True))
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        configure()
    session = _SessionLocal()  # type: ignore[misc]
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
