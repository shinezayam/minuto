"""transcript_revisions: бичвэрийн өөрчлөлтийн түүх

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcript_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("meeting_id", sa.String(36), sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("changed", sa.JSON(), nullable=True),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transcript_revisions_meeting_id", "transcript_revisions", ["meeting_id"])
    op.create_index("ix_transcript_revisions_created_at", "transcript_revisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_transcript_revisions_created_at", table_name="transcript_revisions")
    op.drop_index("ix_transcript_revisions_meeting_id", table_name="transcript_revisions")
    op.drop_table("transcript_revisions")
