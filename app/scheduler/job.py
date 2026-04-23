"""Main cron job orchestrator.

`run_sync(session)`:
  1. Record a `cron_runs` row in `running` state.
  2. Acquire the Postgres advisory lock; exit clean (`skipped`) if held.
  3. Fetch Flex orders from ML since the cursor.
  4. For each order, call `process_order` (two-stage write + optional
     recovery of previously-created SimpliRoute visits).
  5. Finalize the cron_runs row with per-outcome counters.

This module is deliberately thin — all the idempotency logic lives in
`processor.py` and all the HTTP plumbing in `ml/simpliroute` clients.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.alerts import service as alerts
from app.core.logging import logger
from app.db.models import CronRun, CronRunStatus
from app.ml.api import MLClient
from app.ml.oauth import OAuthError
from app.ml.orders import fetch_new_orders, get_cursor
from app.scheduler.lock import LockNotAcquired, advisory_lock
from app.scheduler.processor import Outcome, ProcessResult, process_order
from app.simpliroute.client import SimpliRouteClient


@dataclass
class RunSummary:
    cron_run_id: int
    status: CronRunStatus
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    manual_review: int = 0


def run_sync(
    session: Session,
    *,
    ml_client: MLClient | None = None,
    simpliroute_client: SimpliRouteClient | None = None,
) -> RunSummary:
    cron_run = CronRun(
        started_at=datetime.now(timezone.utc),
        status=CronRunStatus.running,
    )
    session.add(cron_run)
    session.commit()
    cron_run_id = cron_run.id
    logger.info("cron_run_started", cron_run_id=cron_run_id)

    try:
        with advisory_lock(session):
            summary = _execute(
                session,
                cron_run,
                ml_client=ml_client,
                simpliroute_client=simpliroute_client,
            )
    except LockNotAcquired:
        cron_run.status = CronRunStatus.skipped
        cron_run.finished_at = datetime.now(timezone.utc)
        cron_run.notes = "advisory lock held by another worker"
        session.commit()
        logger.info("cron_run_skipped_lock_held", cron_run_id=cron_run_id)
        return RunSummary(cron_run_id=cron_run_id, status=CronRunStatus.skipped)
    except OAuthError as exc:
        cron_run.status = CronRunStatus.failed
        cron_run.finished_at = datetime.now(timezone.utc)
        cron_run.notes = str(exc)[:500]
        session.commit()
        alerts.alert_ml_refresh_failed(session, str(exc))
        logger.exception("cron_run_failed_oauth", cron_run_id=cron_run_id)
        raise
    except Exception as exc:
        cron_run.status = CronRunStatus.failed
        cron_run.finished_at = datetime.now(timezone.utc)
        cron_run.notes = str(exc)[:500]
        session.commit()
        alerts.alert_cron_failed(session, str(exc))
        logger.exception("cron_run_failed", cron_run_id=cron_run_id)
        raise

    cron_run.status = CronRunStatus.success
    cron_run.finished_at = datetime.now(timezone.utc)
    cron_run.processed_count = summary.processed
    cron_run.skipped_count = summary.skipped + summary.manual_review
    cron_run.errors_count = summary.errors
    session.commit()

    logger.info(
        "cron_run_finished",
        cron_run_id=cron_run_id,
        processed=summary.processed,
        skipped=summary.skipped,
        manual_review=summary.manual_review,
        errors=summary.errors,
    )
    summary.cron_run_id = cron_run_id
    summary.status = CronRunStatus.success
    return summary


def _execute(
    session: Session,
    cron_run: CronRun,
    *,
    ml_client: MLClient | None,
    simpliroute_client: SimpliRouteClient | None,
) -> RunSummary:
    summary = RunSummary(cron_run_id=cron_run.id, status=CronRunStatus.running)

    since = get_cursor(session)
    logger.info("cron_run_cursor", since=since.isoformat(), cron_run_id=cron_run.id)

    owns_ml = ml_client is None
    owns_sr = simpliroute_client is None
    ml = ml_client or MLClient(session)
    sr = simpliroute_client or SimpliRouteClient()

    try:
        fetched = fetch_new_orders(session, since=since, client=ml)
        for order in fetched.fetched:
            result = process_order(session, order, sr)
            _count(summary, result)
    finally:
        if owns_ml:
            ml.close()
        if owns_sr:
            sr.close()

    return summary


def _count(summary: RunSummary, result: ProcessResult) -> None:
    outcome = result.outcome
    if outcome in (Outcome.completed, Outcome.recovered):
        summary.processed += 1
    elif outcome == Outcome.skipped_duplicate:
        summary.skipped += 1
    elif outcome == Outcome.manual_review:
        summary.manual_review += 1
    else:  # permanent_failed / transient_failed
        summary.errors += 1
