"""Fetch new Flex orders from Mercado Libre.

Flow:
  1. Query /orders/search filtered by seller + order status + last-updated window.
  2. For each order, fetch /shipments/{id} to read `logistic_type`.
  3. Keep only orders whose shipment has logistic_type == 'self_service' (Flex).
  4. Optionally fetch /users/{buyer_id} when the phone is missing from the order.

The `since` cursor is computed by the caller from the last successful cron run,
with an overlap to tolerate clock skew and to avoid losing orders on the edge.
The DB UNIQUE constraint on ml_order_id is the final dedup safeguard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import service as alerts
from app.core.config import get_settings
from app.core.logging import logger
from app.db.models import CronRun, CronRunStatus
from app.db.models import ManualReview as ManualReviewRow
from app.ml.api import MLClient

PAGE_SIZE = 50
# When there is no prior successful run, look this far back.
DEFAULT_LOOKBACK = timedelta(hours=24)
# Overlap the window to tolerate clock skew.
CURSOR_OVERLAP = timedelta(minutes=15)

# Reason stored on manual_review when a fetched order can't be turned into a
# visit because it carries no shipment (e.g. one leg of a multi-order pack).
REASON_NO_SHIPMENT = "no_shipment_id"


@dataclass
class FetchedOrder:
    order: dict
    shipment: dict
    # Extra buyer profile fields fetched separately, only when needed.
    buyer_profile: dict | None = None

    @property
    def ml_order_id(self) -> str:
        return str(self.order["id"])


@dataclass
class FetchSummary:
    fetched: list[FetchedOrder] = field(default_factory=list)
    skipped_non_flex: int = 0
    skipped_no_shipment: int = 0
    pages: int = 0


def get_cursor(session: Session) -> datetime:
    """Return the `since` datetime for the next ML query.

    Based on the last successful cron_runs row minus an overlap window.
    Falls back to DEFAULT_LOOKBACK when no prior run exists.
    """
    last = session.execute(
        select(CronRun)
        .where(CronRun.status == CronRunStatus.success)
        .order_by(CronRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if last is None:
        return datetime.now(timezone.utc) - DEFAULT_LOOKBACK

    started = last.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started - CURSOR_OVERLAP


def fetch_new_orders(session: Session, *, since: datetime, client: MLClient) -> FetchSummary:
    settings = get_settings()
    if not settings.ml_seller_id:
        raise RuntimeError("ML_SELLER_ID is not configured")

    summary = FetchSummary()
    for order in _iterate_orders(client, seller_id=settings.ml_seller_id, since=since):
        summary.pages = max(summary.pages, 1)
        shipping = order.get("shipping") or {}
        shipment_id = shipping.get("id")
        if not shipment_id:
            # A paid/ready order with no shipment is anomalous — most often one
            # leg of a multi-order pack where only the primary order carries the
            # shipment. This used to be a silent debug log, which is exactly how
            # a sale could vanish with no trace (see the "2 sales, 1 visit"
            # report). Never drop it silently: flag for manual review + alert.
            summary.skipped_no_shipment += 1
            logger.warning(
                "order_dropped_no_shipment",
                ml_order_id=order.get("id"),
                pack_id=order.get("pack_id"),
            )
            _flag_dropped_order(
                session, order, REASON_NO_SHIPMENT, "Order has no shipping.id"
            )
            continue

        shipment = client.get(f"/shipments/{shipment_id}")
        if shipment.get("logistic_type") != "self_service":
            summary.skipped_non_flex += 1
            logger.debug(
                "order_skipped_non_flex",
                ml_order_id=order.get("id"),
                pack_id=order.get("pack_id"),
                logistic_type=shipment.get("logistic_type"),
            )
            continue

        logger.debug(
            "order_fetched_flex",
            ml_order_id=order.get("id"),
            pack_id=order.get("pack_id"),
            shipment_id=shipment_id,
        )
        summary.fetched.append(FetchedOrder(order=order, shipment=shipment))

    logger.info(
        "ml_orders_fetched",
        fetched=len(summary.fetched),
        skipped_non_flex=summary.skipped_non_flex,
        skipped_no_shipment=summary.skipped_no_shipment,
    )
    return summary


def _iterate_orders(
    client: MLClient, *, seller_id: str, since: datetime
) -> Iterator[dict]:
    """Yield orders across all pages of /orders/search."""
    settings = get_settings()
    offset = 0
    # ML expects ISO-8601 like: 2026-04-23T00:00:00.000-00:00
    since_iso = _format_iso(since)

    while True:
        params: dict[str, str | int] = {
            "seller": seller_id,
            "order.status": "paid",
            "order.date_last_updated.from": since_iso,
            "sort": "date_asc",
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        # When triggering on shipment-ready, scope the query further.
        if settings.ml_trigger_status == "ready_to_ship":
            params["shipping.status"] = "ready_to_ship"

        data = client.get("/orders/search", params=params)
        results = data.get("results") or []
        for order in results:
            yield order

        paging = data.get("paging") or {}
        total = int(paging.get("total", len(results)))
        offset += PAGE_SIZE
        if offset >= total or not results:
            break


def _format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Strip microseconds, keep offset.
    return dt.replace(microsecond=0).isoformat()


def _flag_dropped_order(
    session: Session, order: dict, reason: str, detail: str
) -> None:
    """Record a manual_review row (idempotent) + alert for a dropped order.

    Fetch-time drops used to be a silent `debug` log — the reason a sale could
    disappear unnoticed. Surfacing them here guarantees a visible trace on the
    status page (manual_review count) and a Telegram alert.
    """
    ml_order_id = str(order.get("id"))
    existing = session.execute(
        select(ManualReviewRow).where(ManualReviewRow.ml_order_id == ml_order_id)
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ManualReviewRow(
                ml_order_id=ml_order_id,
                reason=reason[:128],
                raw_payload={"order": order, "detail": detail},
            )
        )
        session.commit()
    alerts.alert_manual_review(session, ml_order_id, reason)


# -- On-demand single-order resolution (webhook + QR-scan paths) --


def fetch_order_by_id(client: MLClient, ml_order_id: str) -> FetchedOrder | None:
    """Fetch one order + its shipment, keeping it only if it's Flex.

    Returns None when the order has no shipment or isn't self_service. Used by
    the webhook path, which resolves a single order the moment ML notifies us.
    """
    order = client.get(f"/orders/{ml_order_id}")
    return _build_flex_order(client, order)


def fetch_order_by_shipment(
    client: MLClient, shipment_id: str
) -> FetchedOrder | None:
    """Resolve an order from a shipment id, keeping it only if it's Flex.

    The Flex label QR encodes a shipment id; we read the shipment to find its
    order, then build the same FetchedOrder the cron path produces. Returns
    None when the shipment isn't Flex or has no resolvable order.
    """
    shipment = client.get(f"/shipments/{shipment_id}")
    if shipment.get("logistic_type") != "self_service":
        logger.info(
            "shipment_not_flex",
            shipment_id=shipment_id,
            logistic_type=shipment.get("logistic_type"),
        )
        return None
    order_id = shipment.get("order_id")
    if not order_id:
        orders = shipment.get("orders") or []
        order_id = orders[0].get("id") if orders else None
    if not order_id:
        logger.warning("shipment_without_order", shipment_id=shipment_id)
        return None
    order = client.get(f"/orders/{order_id}")
    return FetchedOrder(order=order, shipment=shipment)


def _build_flex_order(client: MLClient, order: dict) -> FetchedOrder | None:
    shipping = order.get("shipping") or {}
    shipment_id = shipping.get("id")
    if not shipment_id:
        logger.info("single_order_no_shipment", ml_order_id=order.get("id"))
        return None
    shipment = client.get(f"/shipments/{shipment_id}")
    if shipment.get("logistic_type") != "self_service":
        logger.info(
            "single_order_not_flex",
            ml_order_id=order.get("id"),
            logistic_type=shipment.get("logistic_type"),
        )
        return None
    return FetchedOrder(order=order, shipment=shipment)
