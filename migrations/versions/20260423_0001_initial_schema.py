"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-23

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "provider",
            sa.Enum("mercadolibre", name="oauth_provider"),
            nullable=False,
            unique=True,
        ),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "processed_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ml_order_id", sa.String(length=64), nullable=False),
        sa.Column("simpliroute_visit_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "completed", "failed", name="order_status"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_order", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("ml_order_id", name="uq_processed_orders_ml_order_id"),
    )
    op.create_index(
        "ix_processed_orders_status", "processed_orders", ["status"]
    )
    op.create_index(
        "ix_processed_orders_processed_at", "processed_orders", ["processed_at"]
    )

    op.create_table(
        "manual_review",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ml_order_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("ml_order_id", name="uq_manual_review_ml_order_id"),
    )
    op.create_index("ix_manual_review_resolved_at", "manual_review", ["resolved_at"])

    op.create_table(
        "cron_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("running", "success", "failed", "skipped", name="cron_run_status"),
            nullable=False,
        ),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_cron_runs_started_at", "cron_runs", ["started_at"])

    op.create_table(
        "geocoding_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("backend", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("address_hash", name="uq_geocoding_cache_address_hash"),
    )


def downgrade() -> None:
    op.drop_table("geocoding_cache")
    op.drop_index("ix_cron_runs_started_at", table_name="cron_runs")
    op.drop_table("cron_runs")
    op.drop_index("ix_manual_review_resolved_at", table_name="manual_review")
    op.drop_table("manual_review")
    op.drop_index("ix_processed_orders_processed_at", table_name="processed_orders")
    op.drop_index("ix_processed_orders_status", table_name="processed_orders")
    op.drop_table("processed_orders")
    op.drop_table("oauth_tokens")
    op.execute("DROP TYPE IF EXISTS cron_run_status")
    op.execute("DROP TYPE IF EXISTS order_status")
    op.execute("DROP TYPE IF EXISTS oauth_provider")
