from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.ml.flex_label import parse_flex_qr
from app.scheduler.job import run_sync
from app.scheduler.ondemand import process_by_shipment_id
from app.scheduler.watchdog import check_cron_health, check_token_health

router = APIRouter(prefix="/internal", tags=["internal"])


class ScanRequest(BaseModel):
    """Body for /internal/scan. Provide the raw QR text or a shipment id."""

    qr: str | None = None
    shipping_id: str | None = None


def _require_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    expected = get_settings().internal_secret
    if not expected or not x_internal_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing internal secret"
        )
    # Constant-time comparison to avoid timing attacks.
    if not secrets.compare_digest(expected, x_internal_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal secret"
        )


@router.post("/run", dependencies=[Depends(_require_secret)])
def trigger_run(session: Session = Depends(get_db)) -> dict:
    summary = run_sync(session)
    return {
        "cron_run_id": summary.cron_run_id,
        "status": summary.status.value,
        "processed": summary.processed,
        "skipped": summary.skipped,
        "manual_review": summary.manual_review,
        "errors": summary.errors,
    }


@router.post("/scan", dependencies=[Depends(_require_secret)])
def trigger_scan(body: ScanRequest, session: Session = Depends(get_db)) -> dict:
    """Import a Flex visit from a scanned label QR.

    Extracts the shipment id from the QR (or takes it directly), resolves the
    order against ML, and creates the SimpliRoute visit — reusing the same
    idempotent path as the cron. Scanning the same label twice never
    duplicates: the second scan returns outcome=skipped_duplicate.
    """
    shipping_id = body.shipping_id or parse_flex_qr(body.qr)
    if not shipping_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not extract a shipment id from the scan",
        )
    result = process_by_shipment_id(session, shipping_id)
    if not result.found:
        return {"shipping_id": shipping_id, "found": False, "reason": result.reason}
    return {
        "shipping_id": shipping_id,
        "found": True,
        "ml_order_id": result.ml_order_id,
        "outcome": result.outcome,
        "visit_id": result.visit_id,
    }


@router.post("/watchdog", dependencies=[Depends(_require_secret)])
def trigger_watchdog(session: Session = Depends(get_db)) -> dict:
    result = check_cron_health(session)
    return {
        "healthy": result.healthy,
        "minutes_since_last_success": result.minutes_since_last_success,
        "alerted": result.alerted,
    }


@router.post("/token-health", dependencies=[Depends(_require_secret)])
def trigger_token_health(session: Session = Depends(get_db)) -> dict:
    result = check_token_health(session)
    return {
        "refreshed": result.refreshed,
        "age_days": result.age_days,
        "alerted": result.alerted,
    }
