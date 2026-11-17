"""contact_requests.kind/category: Тохиргооноос ирэх санал хүсэлтийг Enterprise хүсэлттэй нэг хүснэгтэд хадгална

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Хуучин мөрүүд бүгд Enterprise хүсэлт
    with op.batch_alter_table("contact_requests") as b:
        b.add_column(sa.Column("kind", sa.String(16), nullable=False, server_default="enterprise"))
        b.add_column(sa.Column("category", sa.String(16), nullable=True))
        b.create_index("ix_contact_requests_kind", ["kind"])


def downgrade() -> None:
    with op.batch_alter_table("contact_requests") as b:
        b.drop_index("ix_contact_requests_kind")
        b.drop_column("category")
        b.drop_column("kind")
