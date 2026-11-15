"""Alembic-ийн програмчилсан интерфейс + CLI.

  python -m app.migrate current | history | upgrade [rev=head] | downgrade <rev>
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_LOCK_KEY = 727_001  # Postgres advisory lock: хоёр instance зэрэг migrate хийхгүй


def _config(conn: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    return cfg


def head_revision() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head() or ""


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _run(engine: Engine, fn) -> None:
    with engine.connect() as conn:
        postgres = conn.dialect.name == "postgresql"
        if postgres:
            conn.exec_driver_sql(f"SELECT pg_advisory_lock({_LOCK_KEY})")
            conn.commit()
        try:
            fn(_config(conn))
            conn.commit()
        finally:
            if postgres:
                conn.rollback()
                conn.exec_driver_sql(f"SELECT pg_advisory_unlock({_LOCK_KEY})")
                conn.commit()


def upgrade_to_head(engine: Engine, revision: str = "head") -> None:
    before = current_revision(engine)
    _run(engine, lambda cfg: command.upgrade(cfg, revision))
    after = current_revision(engine)
    if before != after:
        logger.info("Migrated database %s → %s", before, after)


def downgrade(engine: Engine, revision: str) -> None:
    _run(engine, lambda cfg: command.downgrade(cfg, revision))


def main(argv: list[str]) -> int:
    from sqlalchemy import create_engine

    from app.config import settings
    from app.db import normalize_url

    logging.basicConfig(level=logging.INFO)
    url = normalize_url(settings.database_url or "sqlite:///./minuto.db")
    engine = create_engine(url)
    cmd = argv[0] if argv else "current"
    if cmd == "current":
        print(current_revision(engine), "(head:", head_revision() + ")")
    elif cmd == "history":
        _run(engine, lambda cfg: command.history(cfg, verbose=False))
    elif cmd == "upgrade":
        upgrade_to_head(engine, argv[1] if len(argv) > 1 else "head")
        print("now at", current_revision(engine))
    elif cmd == "downgrade":
        if len(argv) < 2:
            print("Usage: downgrade <revision>")
            return 2
        downgrade(engine, argv[1])
        print("now at", current_revision(engine))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
