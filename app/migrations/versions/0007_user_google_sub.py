"""users.google_sub: "Google-ээр нэвтрэх" бүртгэлийн тогтмол Google ID (и-мэйл солигдсон ч таних)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("google_sub", sa.String(64), nullable=True))
        batch.create_index("uq_users_google_sub", ["google_sub"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_index("uq_users_google_sub")
        batch.drop_column("google_sub")
