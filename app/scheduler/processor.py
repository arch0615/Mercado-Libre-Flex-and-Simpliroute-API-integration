"""Single-order processing with two-stage write.

The layers of defense against duplicates:
  1. UNIQUE(ml_order_id) on `processed_orders` — ultimate DB safeguard.
  2. Two-stage write: claim as `pending` + COMMIT, then call SimpliRoute,
     then UPDATE to `completed`. Any crash in between leaves a `pending`
     row that is retried on the next cron run.
  3. On retry, `find_visit_by_reference` queries SimpliRoute by the ML
     order id we stored as the visit's external reference. If a visit
     already exists for this order, we reuse it instead of creating a
     new one — covers the crash-between-POST-and-UPDATE window.
  4. `reference = ml_order_id` on every visit — trace + audit from the
     SimpliRoute side.
  5. Advisory lock in the caller prevents concurrent runs from racing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.core.transform import ManualReview, VisitPayload, map_order_to_visit
from app.db.models import ManualReview as ManualReviewRow
from app.db.models import OrderStatus, ProcessedOrder
from app.geocoder import service as geocoder_service
from app.geocoder.base import Geocoder
from app.ml.orders import FetchedOrder
from app.simpliroute.client import (
    PermanentSimpliRouteError,
    SimpliRouteClient,
    TransientSimpliRouteError,
)


class Outcome(str, Enum):
    completed = "completed"
    skipped_duplicate = "skipped_duplicate"
    recovered = "recovered"  # existing SimpliRoute visit rebound
    manual_review = "manual_review"
    permanent_failed = "permanent_failed"
    transient_failed = "transient_failed"


@dataclass
class ProcessResult:
    outcome: Outcome
    ml_order_id: str
    visit_id: str | None = None
    reason: str | None = None
    detail: str | None = None


REASON_GEOCODE_FAILED = "geocode_failed"
REASON_GEOCODE_LOW_CONFIDENCE = "geocode_low_confidence"
REASON_SIMPLIROUTE_PERMANENT = "simpliroute_permanent_error"


def process_order(
    session: Session,
    fetched: FetchedOrder,
    simpliroute_client: SimpliRouteClient,
    *,
    geocoder: Geocoder | None = None,
) -> ProcessResult:
    ml_order_id = fetched.ml_order_id

    # 0) Map ML -> VisitPayload; bail to manual_review on missing fields.
    mapped = map_order_to_visit(fetched)
    if isinstance(mapped, ManualReview):
        _record_manual_review(session, ml_order_id, mapped.reason, mapped.detail, fetched.order)
        return ProcessResult(
            outcome=Outcome.manual_review,
            ml_order_id=ml_order_id,
            reason=mapped.reason,
            detail=mapped.detail,
        )
    visit: VisitPayload = mapped

    # 1) Geocode when ML didn't provide coordinates.
    if visit.latitude is None or visit.longitude is None:
        reason = _resolve_coordinates(session, visit, geocoder=geocoder)
        if reason is not None:
            _record_manual_review(session, ml_order_id, reason, visit.address, fetched.order)
            return ProcessResult(
                outcome=Outcome.manual_review,
                ml_order_id=ml_order_id,
                reason=reason,
                detail=visit.address,
            )

    # 2) Claim or reuse the processed_orders row.
    claim = _claim_or_retry(session, ml_order_id, fetched.order)
    if claim == "skip":
        logger.info("order_skipped_already_completed", ml_order_id=ml_order_id)
        return ProcessResult(outcome=Outcome.skipped_duplicate, ml_order_id=ml_order_id)

    # 2b) On retry, check SimpliRoute for a visit already created in a prior
    # crashed run. If found, adopt it and skip the create call.
    if claim == "retry":
        try:
            existing = simpliroute_client.find_visit_by_reference(ml_order_id)
        except TransientSimpliRouteError:
            existing = None  # fall through to normal create with retries
        if existing:
            visit_id = str(existing.get("id") or existing.get("pk"))
            _mark_completed(session, ml_order_id, visit_id)
            logger.info(
                "order_recovered_existing_visit",
                ml_order_id=ml_order_id,
                visit_id=visit_id,
            )
            return ProcessResult(
                outcome=Outcome.recovered,
                ml_order_id=ml_order_id,
                visit_id=visit_id,
            )

    # 3) Create the SimpliRoute visit.
    try:
        response = simpliroute_client.create_visit(visit)
    except PermanentSimpliRouteError as e:
        _mark_failed(session, ml_order_id, str(e)[:1000])
        _record_manual_review(
            session, ml_order_id, REASON_SIMPLIROUTE_PERMANENT, str(e)[:1000], fetched.order
        )
        logger.error("simpliroute_permanent_failure", ml_order_id=ml_order_id, error=str(e))
        return ProcessResult(
            outcome=Outcome.permanent_failed,
            ml_order_id=ml_order_id,
            reason=REASON_SIMPLIROUTE_PERMANENT,
            detail=str(e),
        )
    except TransientSimpliRouteError as e:
        _mark_failed(session, ml_order_id, str(e)[:1000])
        logger.warning("simpliroute_transient_exhausted", ml_order_id=ml_order_id, error=str(e))
        return ProcessResult(
            outcome=Outcome.transient_failed, ml_order_id=ml_order_id, detail=str(e)
        )

    # 4) Finalize: mark completed with the visit id.
    visit_id = str(response.get("id") or response.get("pk") or "")
    _mark_completed(session, ml_order_id, visit_id)
    logger.info("order_completed", ml_order_id=ml_order_id, visit_id=visit_id)
    return ProcessResult(
        outcome=Outcome.completed, ml_order_id=ml_order_id, visit_id=visit_id
    )


# -- Internal helpers --


ClaimResult = Literal["new", "retry", "skip"]


def _claim_or_retry(session: Session, ml_order_id: str, raw_order: dict) -> ClaimResult:
    """Atomically INSERT a pending row, or inspect the existing row.

    - 'new':    row did not exist; freshly inserted as pending.
    - 'retry':  row exists but is not completed (pending / failed) — reprocess.
    - 'skip':   row exists and is completed — idempotent skip.

    Commits the claim immediately so other concurrent workers (if the
    advisory lock were ever bypassed) would observe the pending row.
    """
    savepoint = session.begin_nested()
    try:
        session.add(
            ProcessedOrder(
                ml_order_id=ml_order_id,
                status=OrderStatus.pending,
                processed_at=datetime.now(timezone.utc),
                raw_order=raw_order,
            )
        )
        session.flush()
        savepoint.commit()
        session.commit()
        return "new"
    except IntegrityError:
        savepoint.rollback()

    existing = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == ml_order_id)
    ).scalar_one()
    if existing.status == OrderStatus.completed:
        return "skip"

    existing.processed_at = datetime.now(timezone.utc)
    session.commit()
    return "retry"


def _mark_completed(session: Session, ml_order_id: str, visit_id: str) -> None:
    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == ml_order_id)
    ).scalar_one()
    row.status = OrderStatus.completed
    row.simpliroute_visit_id = visit_id
    row.completed_at = datetime.now(timezone.utc)
    row.error = None
    session.commit()


def _mark_failed(session: Session, ml_order_id: str, error: str) -> None:
    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == ml_order_id)
    ).scalar_one()
    row.status = OrderStatus.failed
    row.error = error
    row.retries = (row.retries or 0) + 1
    session.commit()


def _record_manual_review(
    session: Session, ml_order_id: str, reason: str, detail: str | None, raw: dict
) -> None:
    existing = session.execute(
        select(ManualReviewRow).where(ManualReviewRow.ml_order_id == ml_order_id)
    ).scalar_one_or_none()
    if existing is not None:
        return  # already flagged; don't overwrite original reason
    session.add(
        ManualReviewRow(
            ml_order_id=ml_order_id,
            reason=reason[:128],
            raw_payload={"order": raw, "detail": detail},
        )
    )
    session.commit()


def _resolve_coordinates(
    session: Session, visit: VisitPayload, *, geocoder: Geocoder | None
) -> str | None:
    """Populate visit.lat/lng via the geocoder. Return reason on failure."""
    settings = get_settings()
    result = geocoder_service.resolve(session, visit.address, backend=geocoder)
    if result is None:
        return REASON_GEOCODE_FAILED
    if result.confidence < settings.geocoder_min_confidence:
        logger.warning(
            "geocode_below_threshold",
            address=visit.address,
            confidence=result.confidence,
            threshold=settings.geocoder_min_confidence,
        )
        return REASON_GEOCODE_LOW_CONFIDENCE
    visit.latitude = result.lat
    visit.longitude = result.lng
    return None
