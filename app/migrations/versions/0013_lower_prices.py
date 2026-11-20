"""plans: үнэ бууруулах (Starter 9,900₮, Pro 24,900₮, Team 59,900₮; минут хэвээр)

Одоо идэвхтэй захиалга, үүссэн төлбөрийн захиалгад нөлөөлөхгүй — зөвхөн шинэ checkout шинэ үнээр.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

NEW = {"starter": 9900, "pro": 24900, "team": 59900}
OLD = {"starter": 29900, "pro": 69900, "team": 149900}


def _set(prices: dict[str, int]) -> None:
    stmt = sa.text("UPDATE plans SET price_mnt = :price WHERE code = :code")
    for code, price in prices.items():
        op.execute(stmt.bindparams(price=price, code=code))


def upgrade() -> None:
    _set(NEW)


def downgrade() -> None:
    _set(OLD)
