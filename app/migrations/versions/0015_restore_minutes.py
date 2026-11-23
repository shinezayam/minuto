"""plans: минутыг хуучин хэмжээнд буцаах (Free 30, Starter 120, Pro 360, Team 900; үнэ 0013-ийнх хэвээр)

Шинэ үнэ (9,900 / 24,900 / 59,900₮) дээр 0014-ийн минутаар API зардал ашгийг хэт бууруулж байсан.
Үлдэгдлийг plans.minutes-ээс шууд уншдаг тул одоо идэвхтэй захиалгууд ч шинэ (бага) минутыг даруй авна.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

NEW = {"free": 30, "starter": 120, "pro": 360, "team": 900}
OLD = {"free": 60, "starter": 180, "pro": 480, "team": 1200}


def _set(minutes: dict[str, int]) -> None:
    stmt = sa.text("UPDATE plans SET minutes = :minutes WHERE code = :code")
    for code, n in minutes.items():
        op.execute(stmt.bindparams(minutes=n, code=code))


def upgrade() -> None:
    _set(NEW)


def downgrade() -> None:
    _set(OLD)
