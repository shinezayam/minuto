"""admin_grants: үндсэн админ (ADMIN_EMAILS) сайтаас нэмсэн админууд

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_grants",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("added_by", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("admin_grants")
