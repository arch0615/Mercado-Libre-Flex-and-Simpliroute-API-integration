from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class OAuthProvider(str, Enum):
    mercadolibre = "mercadolibre"


class OrderStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"


class CronRunStatus(str, Enum):
    running = "running"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class OAuthToken(Base, TimestampMixin):
    """Stores OAuth tokens, one row per provider. Rotates on every refresh."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[OAuthProvider] = mapped_column(
        SAEnum(OAuthProvider, name="oauth_provider"), unique=True, nullable=False
    )
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Account identifier returned by the provider (ML user_id).
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Scope granted on the last exchange, informational.
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProcessedOrder(Base, TimestampMixin):
    """One row per ML order. UNIQUE(ml_order_id) is the ultimate dedup safeguard."""

    __tablename__ = "processed_orders"
    __table_args__ = (
        UniqueConstraint("ml_order_id", name="uq_processed_orders_ml_order_id"),
        Index("ix_processed_orders_status", "status"),
        Index("ix_processed_orders_processed_at", "processed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ml_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    simpliroute_visit_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"), nullable=False, default=OrderStatus.pending
    )
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_order: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ManualReview(Base, TimestampMixin):
    """Orders that cannot be auto-processed (bad address, missing fields, etc.)."""

    __tablename__ = "manual_review"
    __table_args__ = (
        UniqueConstraint("ml_order_id", name="uq_manual_review_ml_order_id"),
        Index("ix_manual_review_resolved_at", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ml_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CronRun(Base, TimestampMixin):
    """Audit trail of every scheduler invocation."""

    __tablename__ = "cron_runs"
    __table_args__ = (Index("ix_cron_runs_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[CronRunStatus] = mapped_column(
        SAEnum(CronRunStatus, name="cron_run_status"), nullable=False
    )
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class GeocodingCache(Base, TimestampMixin):
    """Normalized-address -> coordinates cache, avoids repeat Google calls."""

    __tablename__ = "geocoding_cache"
    __table_args__ = (
        UniqueConstraint("address_hash", name="uq_geocoding_cache_address_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_address: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
