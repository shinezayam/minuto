"""billing: plans, subscriptions, usage_events (минут тоолох), payment_orders (Wire/QPay), contact_requests

Бүгд шинэ хүснэгт — одоо байгаа хүснэгт, баганыг өөрчлөхгүй. BILLING_METERING=off (default) үед код эдгээрт хүрэхгүй.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_LIVE = sa.text("state IN ('reserved','debited')")
_ACTIVE_SUB = sa.text("status = 'active'")

# Анхны үнийн хүснэгт (0013-аар бууруулсан). Өөрчлөхөд шинэ migration + test_billing-ийн үнийн тест.
PLANS = [
    {"code": "free", "name": "Free", "kind": "free", "minutes": 30, "period_days": None, "price_mnt": 0, "scope": "user", "sort": 0},
    {"code": "starter", "name": "Starter", "kind": "paid", "minutes": 120, "period_days": 30, "price_mnt": 29900, "scope": "user", "sort": 1},
    {"code": "pro", "name": "Pro", "kind": "paid", "minutes": 360, "period_days": 30, "price_mnt": 69900, "scope": "user", "sort": 2},
    {"code": "team", "name": "Team", "kind": "paid", "minutes": 900, "period_days": 30, "price_mnt": 149900, "scope": "workspace", "sort": 3},
    {"code": "enterprise", "name": "Enterprise", "kind": "custom", "minutes": None, "period_days": None, "price_mnt": None, "scope": "workspace", "sort": 4},
]


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("price_mnt", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.bulk_insert(plans, [dict(p, active=True) for p in PLANS])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True),
        sa.Column("plan_code", sa.String(20), sa.ForeignKey("plans.code"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("order_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(user_id IS NULL) <> (workspace_id IS NULL)", name="ck_subscription_one_owner"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_workspace_id", "subscriptions", ["workspace_id"])
    op.create_index("ix_subscriptions_period_end", "subscriptions", ["period_end"])
    op.create_index("uq_subscriptions_active_user", "subscriptions", ["user_id"], unique=True,
                    sqlite_where=_ACTIVE_SUB, postgresql_where=_ACTIVE_SUB)
    op.create_index("uq_subscriptions_active_workspace", "subscriptions", ["workspace_id"], unique=True,
                    sqlite_where=_ACTIVE_SUB, postgresql_where=_ACTIVE_SUB)
    op.create_index("uq_subscriptions_order", "subscriptions", ["order_id"], unique=True)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        # Ledger: FK байхгүй — хурал/хэрэглэгч устсан ч хэрэглээний түүх үлдэнэ
        sa.Column("scope_type", sa.String(10), nullable=False),
        sa.Column("scope_id", sa.String(36), nullable=False),
        sa.Column("subscription_id", sa.String(36), nullable=True),
        sa.Column("meeting_id", sa.String(36), nullable=True),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("plan_code", sa.String(20), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("audio_seconds", sa.Integer(), nullable=True),
        sa.Column("duration_source", sa.String(10), nullable=False),
        sa.Column("billable_minutes", sa.Integer(), nullable=False),
        sa.Column("audio_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("shadow", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("remaining_before", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_usage_events_scope", "usage_events", ["scope_type", "scope_id", "created_at"])
    op.create_index("ix_usage_events_state", "usage_events", ["state"])
    op.create_index("ix_usage_events_meeting_id", "usage_events", ["meeting_id"])
    op.create_index("uq_usage_events_audio", "usage_events", ["meeting_id", "audio_fingerprint"], unique=True,
                    sqlite_where=_LIVE, postgresql_where=_LIVE)

    op.create_table(
        "payment_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plan_code", sa.String(20), sa.ForeignKey("plans.code"), nullable=False),
        sa.Column("amount_mnt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_ref", sa.String(100), nullable=True),
        sa.Column("payment", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payment_orders_user_id", "payment_orders", ["user_id"])
    op.create_index("ix_payment_orders_status", "payment_orders", ["status"])
    op.create_index("uq_payment_orders_provider_ref", "payment_orders", ["provider", "provider_ref"], unique=True)

    op.create_table(
        "contact_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("company", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_contact_requests_created_at", "contact_requests", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_contact_requests_created_at", table_name="contact_requests")
    op.drop_table("contact_requests")
    op.drop_index("uq_payment_orders_provider_ref", table_name="payment_orders")
    op.drop_index("ix_payment_orders_status", table_name="payment_orders")
    op.drop_index("ix_payment_orders_user_id", table_name="payment_orders")
    op.drop_table("payment_orders")
    op.drop_index("uq_usage_events_audio", table_name="usage_events")
    op.drop_index("ix_usage_events_meeting_id", table_name="usage_events")
    op.drop_index("ix_usage_events_state", table_name="usage_events")
    op.drop_index("ix_usage_events_scope", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("uq_subscriptions_order", table_name="subscriptions")
    op.drop_index("uq_subscriptions_active_workspace", table_name="subscriptions")
    op.drop_index("uq_subscriptions_active_user", table_name="subscriptions")
    op.drop_index("ix_subscriptions_period_end", table_name="subscriptions")
    op.drop_index("ix_subscriptions_workspace_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
