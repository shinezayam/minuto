"""plans: минут нэмэх (Free 60, Starter 180, Pro 480, Team 1,200; үнэ хэвээр)

Үлдэгдлийг plans.minutes-ээс шууд уншдаг тул одоо идэвхтэй захиалгууд ч шинэ минутыг даруй авна.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

NEW = {"free": 60, "starter": 180, "pro": 480, "team": 1200}
OLD = {"free": 30, "starter": 120, "pro": 360, "team": 900}


def _set(minutes: dict[str, int]) -> None:
    stmt = sa.text("UPDATE plans SET minutes = :minutes WHERE code = :code")
    for code, n in minutes.items():
        op.execute(stmt.bindparams(minutes=n, code=code))


def upgrade() -> None:
    _set(NEW)


def downgrade() -> None:
    _set(OLD)
