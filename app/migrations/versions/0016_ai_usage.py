"""ai_usage: гадны AI API-ийн хэрэглээ, зардал (Scribe аудио секунд, Qwen токен) — админ хуудсанд харуулна

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meeting_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(60), nullable=False, server_default=""),
        sa.Column("audio_seconds", sa.Float, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
    )
    op.create_index("ix_ai_usage_created_at", "ai_usage", ["created_at"])
    op.create_index("ix_ai_usage_meeting_id", "ai_usage", ["meeting_id"])
    op.create_index("ix_ai_usage_user_id", "ai_usage", ["user_id"])


def downgrade() -> None:
    op.drop_table("ai_usage")
