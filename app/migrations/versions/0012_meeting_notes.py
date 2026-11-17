"""meetings.notes: хөтлөгчийн гар тэмдэглэл (аюулгүй HTML)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("meetings") as b:
        b.add_column(sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("meetings") as b:
        b.drop_column("notes")
