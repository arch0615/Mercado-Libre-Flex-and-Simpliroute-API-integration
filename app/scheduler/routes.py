from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.scheduler.job import run_sync
from app.scheduler.watchdog import check_cron_health, check_token_health

router = APIRouter(prefix="/internal", tags=["internal"])


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
