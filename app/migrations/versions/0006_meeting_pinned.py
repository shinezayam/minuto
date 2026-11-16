"""meetings.pinned_at: "Анхаарах" (яаралтай) хурлыг жагсаалтын дээр гаргах

Nullable багана нэмнэ — одоо байгаа мөрүүд өөрчлөгдөхгүй.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("meetings") as batch:
        batch.add_column(sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("meetings") as batch:
        batch.drop_column("pinned_at")
