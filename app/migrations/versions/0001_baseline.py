"""baseline: одоогийн схем (байгаа DB дээр юу ч өөрчлөхгүй)

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

from app.migrations import baseline_schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Хүснэгт байхгүй бол үүсгэнэ, байгаа бол орхино (checkfirst) — production-ийн одоогийн DB дээр no-op
    baseline_schema.metadata.create_all(bind, checkfirst=True)
    # Хуучин (create_all-ын өмнөх үеийн) DB-д дутуу баганыг нэмнэ — байгаа бол алгасна
    insp = sa.inspect(bind)
    for table, column, ddl in baseline_schema.LEGACY_COLUMNS:
        if column not in {c["name"] for c in insp.get_columns(table)}:
            op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def downgrade() -> None:
    # Baseline-аас доош буулгах нь бүх өгөгдлийг устгана — зориудаар дэмжихгүй
    raise RuntimeError("Baseline-ийг downgrade хийх боломжгүй (өгөгдөл устна). Backup-аас сэргээнэ үү.")
