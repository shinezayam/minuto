"""users.token_version: токеныг хүчингүй болгох (нууц үг солих, бүх төхөөрөмжөөс гаргах)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("token_version")
