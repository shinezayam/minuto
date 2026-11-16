"""audio_purges: Supabase-аас устгаж чадаагүй аудионы дараалал (сервер дахин эхэлсэн ч алга болохгүй)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audio_purges",
        sa.Column("meeting_id", sa.String(36), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audio_purges")
