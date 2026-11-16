"""processing_jobs: боловсруулалтын durable ажлын дараалал

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_ACTIVE = sa.text("state IN ('queued','running')")


def upgrade() -> None:
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("meeting_id", sa.String(36), sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(20), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("chunks", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_processing_jobs_meeting_id", "processing_jobs", ["meeting_id"])
    op.create_index("ix_processing_jobs_state", "processing_jobs", ["state"])
    op.create_index(
        "uq_processing_jobs_active_meeting", "processing_jobs", ["meeting_id"], unique=True,
        sqlite_where=_ACTIVE, postgresql_where=_ACTIVE,
    )


def downgrade() -> None:
    op.drop_index("uq_processing_jobs_active_meeting", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_state", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_meeting_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")
