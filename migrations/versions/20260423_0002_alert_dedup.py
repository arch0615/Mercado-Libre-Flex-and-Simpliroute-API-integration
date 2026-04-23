"""alert_dedup table

Revision ID: 0002_alert_dedup
Revises: 0001_initial
Create Date: 2026-04-23

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_alert_dedup"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_dedup",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_title", sa.Text(), nullable=True),
        sa.Column("last_level", sa.String(length=16), nullable=True),
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
        sa.UniqueConstraint("dedup_key", name="uq_alert_dedup_key"),
    )


def downgrade() -> None:
    op.drop_table("alert_dedup")
