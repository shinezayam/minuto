"""meetings.deleted_at: төлбөртэй багцтай хэрэглэгчийн устгасан хурал "Хогийн сав"-д 30 хоног хадгалагдана

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("meetings") as b:
        b.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        b.create_index("ix_meetings_deleted_at", ["deleted_at"])


def downgrade() -> None:
    with op.batch_alter_table("meetings") as b:
        b.drop_index("ix_meetings_deleted_at")
        b.drop_column("deleted_at")
