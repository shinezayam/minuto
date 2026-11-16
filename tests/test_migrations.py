from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app import migrate
from app.db import Base
from app.migrations import baseline_schema


def _engine(tmp_path, name="m.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def _cols(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_fresh_database_reaches_head(tmp_path):
    e = _engine(tmp_path)
    migrate.upgrade_to_head(e)
    assert migrate.current_revision(e) == migrate.head_revision() == "0008"
    assert "token_version" in _cols(e, "users")


def test_models_and_migrations_do_not_drift(tmp_path):
    """Модель өөрчлөгдсөн боловч migration бичээгүй бол энэ тест унана."""
    e = _engine(tmp_path)
    migrate.upgrade_to_head(e)
    with e.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"Migration дутуу: {diff}"


def test_upgrade_is_idempotent(tmp_path):
    e = _engine(tmp_path)
    migrate.upgrade_to_head(e)
    migrate.upgrade_to_head(e)
    assert migrate.current_revision(e) == "0008"


def test_existing_pre_alembic_database_keeps_data_and_gets_stamped(tmp_path):
    """Production-ийн одоогийн DB: хүснэгт бүгд байгаа, alembic_version байхгүй, token_version байхгүй."""
    e = _engine(tmp_path)
    baseline_schema.metadata.create_all(e)
    with e.begin() as c:
        c.execute(text("INSERT INTO users (id, email, password_hash, created_at) VALUES ('u1','a@b.mn','x','2026-01-01')"))
    migrate.upgrade_to_head(e)
    assert migrate.current_revision(e) == "0008"
    with e.connect() as c:
        row = c.execute(text("SELECT email, token_version FROM users WHERE id='u1'")).one()
    assert tuple(row) == ("a@b.mn", 0)  # өгөгдөл хэвээр, шинэ багана default 0


def test_legacy_database_missing_old_columns_is_repaired(tmp_path):
    """create_all()-ын өмнөх үеийн DB: users.name, meetings.participants гэх мэт байхгүй."""
    e = _engine(tmp_path)
    baseline_schema.metadata.create_all(e)
    with e.begin() as c:
        c.execute(text("ALTER TABLE users DROP COLUMN name"))
        c.execute(text("ALTER TABLE meetings DROP COLUMN participants"))
    migrate.upgrade_to_head(e)
    assert "name" in _cols(e, "users") and "participants" in _cols(e, "meetings")


def test_downgrade_of_each_new_revision(tmp_path):
    e = _engine(tmp_path)
    migrate.upgrade_to_head(e)
    tables = lambda: set(inspect(e).get_table_names())  # noqa: E731
    billing_tables = {"plans", "subscriptions", "usage_events", "payment_orders", "contact_requests"}
    assert {"transcript_revisions", "processing_jobs"} | billing_tables <= tables()

    assert "audio_purges" in tables()
    migrate.downgrade(e, "0007")  # 0008 → 0007: зөвхөн audio_purges устна
    assert migrate.current_revision(e) == "0007" and "audio_purges" not in tables()

    assert "google_sub" in _cols(e, "users")
    migrate.downgrade(e, "0006")  # 0007 → 0006: зөвхөн users.google_sub устна
    assert migrate.current_revision(e) == "0006" and "google_sub" not in _cols(e, "users")

    assert "pinned_at" in _cols(e, "meetings")
    migrate.downgrade(e, "0005")  # 0006 → 0005: зөвхөн meetings.pinned_at устна
    assert migrate.current_revision(e) == "0005" and "pinned_at" not in _cols(e, "meetings")

    migrate.downgrade(e, "0004")  # 0005 → 0004: зөвхөн billing хүснэгтүүд устна
    assert migrate.current_revision(e) == "0004"
    assert not (billing_tables & tables()) and "processing_jobs" in tables()

    migrate.downgrade(e, "0003")  # 0004 → 0003
    assert migrate.current_revision(e) == "0003"
    assert "processing_jobs" not in tables() and "transcript_revisions" in tables()

    migrate.downgrade(e, "0002")  # 0003 → 0002
    assert migrate.current_revision(e) == "0002"
    assert "transcript_revisions" not in tables() and "token_version" in _cols(e, "users")

    migrate.downgrade(e, "0001")  # 0002 → 0001
    assert migrate.current_revision(e) == "0001"
    assert "token_version" not in _cols(e, "users")

    migrate.upgrade_to_head(e)  # дахин дээш гарна
    assert migrate.current_revision(e) == "0008"
    assert "token_version" in _cols(e, "users") and {"transcript_revisions", "processing_jobs"} <= tables()


def test_baseline_cannot_be_downgraded(tmp_path):
    e = _engine(tmp_path)
    migrate.upgrade_to_head(e)
    with pytest.raises(Exception, match="Baseline"):
        migrate.downgrade(e, "base")
    assert "users" in inspect(e).get_table_names()  # өгөгдөл устаагүй


def test_app_startup_migrates_database(tmp_path):
    from app import db

    eng = db.configure(f"sqlite:///{tmp_path / 'app.db'}")
    assert migrate.current_revision(eng) == "0008"
