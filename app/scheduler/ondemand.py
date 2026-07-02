"""On-demand single-order processing for the webhook + QR-scan paths.

Both entry points resolve exactly one order and run it through the same
`process_order` two-stage write the cron uses, so they inherit every
idempotency guarantee for free: UNIQUE(ml_order_id), claim-before-call,
and reference-based recovery.

We deliberately skip the global advisory lock here. The per-order UNIQUE
claim in `_claim_or_retry` already makes concurrent processing of the same
order safe (a racing worker gets an IntegrityError and falls into the retry
path). Skipping the lock is precisely what lets a webhook create a visit the
instant the order lands instead of queueing behind a running cron — which is
the whole point of the low-latency path.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.logging import logger
from app.ml.api import MLClient
from app.ml.orders import (
    FetchedOrder,
    fetch_order_by_id,
    fetch_order_by_shipment,
)
from app.scheduler.processor import process_order
from app.simpliroute.client import SimpliRouteClient


@dataclass
class OnDemandResult:
    """Outcome of resolving + processing a single order on demand."""

    found: bool
    ml_order_id: str | None = None
    outcome: str | None = None
    visit_id: str | None = None
    reason: str | None = None


def process_by_order_id(
    session: Session,
    ml_order_id: str,
    *,
    ml_client: MLClient | None = None,
    sr_client: SimpliRouteClient | None = None,
) -> OnDemandResult:
    """Resolve and process one order by its ML order id (webhook order topic)."""
    return _run(
        session,
        lambda ml: fetch_order_by_id(ml, ml_order_id),
        ml_client=ml_client,
        sr_client=sr_client,
        not_found_reason="order_not_flex_or_missing_shipment",
    )


def process_by_shipment_id(
    session: Session,
    shipment_id: str,
    *,
    ml_client: MLClient | None = None,
    sr_client: SimpliRouteClient | None = None,
) -> OnDemandResult:
    """Resolve and process one order by shipment id (QR scan + shipment topic)."""
    return _run(
        session,
        lambda ml: fetch_order_by_shipment(ml, shipment_id),
        ml_client=ml_client,
        sr_client=sr_client,
        not_found_reason="shipment_not_flex_or_no_order",
    )


def _run(
    session: Session,
    fetch: Callable[[MLClient], FetchedOrder | None],
    *,
    ml_client: MLClient | None,
    sr_client: SimpliRouteClient | None,
    not_found_reason: str,
) -> OnDemandResult:
    owns_ml = ml_client is None
    owns_sr = sr_client is None
    ml = ml_client or MLClient(session)
    sr = sr_client or SimpliRouteClient()
    try:
        fetched = fetch(ml)
        if fetched is None:
            logger.info("ondemand_not_processable", reason=not_found_reason)
            return OnDemandResult(found=False, reason=not_found_reason)
        result = process_order(session, fetched, sr)
        return OnDemandResult(
            found=True,
            ml_order_id=result.ml_order_id,
            outcome=result.outcome.value,
            visit_id=result.visit_id,
            reason=result.reason,
        )
    finally:
        if owns_ml:
            ml.close()
        if owns_sr:
            sr.close()
