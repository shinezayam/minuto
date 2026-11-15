"""Alembic орчин. Үргэлж аппын өөрийн холболтоор (config.attributes["connection"]) ажиллана."""
from __future__ import annotations

from alembic import context

from app.db import Base

config = context.config
connection = config.attributes.get("connection")


def run_migrations_online() -> None:
    if connection is None:
        raise RuntimeError("Migration-ийг `python -m app.migrate ...` эсвэл app.migrate.upgrade_to_head() -ээр ажиллуулна")
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite ALTER хязгаарлалт
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) горим дэмжигдэхгүй")
run_migrations_online()
