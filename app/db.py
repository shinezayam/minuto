"""Мэдээллийн сан: DATABASE_URL (Postgres) байвал түүнийг, үгүй бол локал SQLite файлыг ашиглана."""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect, text
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
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    logger.info("Database ready: %s", _engine.url.render_as_string(hide_password=True))
    return _engine


# create_all() байгаа хүснэгтэд шинэ багана нэмдэггүй тул энд жагсааж нэмнэ (Postgres, SQLite хоёуланд)
_ADDED_COLUMNS = [
    ("users", "name", "VARCHAR(100)"),
    ("meetings", "participants", "JSON"),
    ("meetings", "archived", "INTEGER DEFAULT 0"),
]


def _add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    for table, column, ddl in _ADDED_COLUMNS:
        if column not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            logger.info("Added column %s.%s", table, column)


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
