"""HTML views for the operator-facing surfaces.

Read-only by design: no actions, no auth, no charts. Just a polished
front for the few pages a human will actually see (landing + OAuth flow
+ status). Anything that would change state lives behind /internal/* or
the cron job.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.db.models import (
    AlertDedup,
    CronRun,
    CronRunStatus,
    ManualReview,
    OAuthProvider,
    OAuthToken,
    OrderStatus,
    ProcessedOrder,
)
from app.web.templates import templates

router = APIRouter(tags=["web"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    last_run = _last_successful_run(session)
    processed_total = session.execute(
        select(func.count(ProcessedOrder.id)).where(
            ProcessedOrder.status == OrderStatus.completed
        )
    ).scalar() or 0
    processed_today = session.execute(
        select(func.count(ProcessedOrder.id)).where(
            ProcessedOrder.status == OrderStatus.completed,
            ProcessedOrder.completed_at >= _start_of_today(),
        )
    ).scalar() or 0
    manual_review_open = session.execute(
        select(func.count(ManualReview.id)).where(
            ManualReview.resolved_at.is_(None)
        )
    ).scalar() or 0

    healthy = last_run is not None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "now": _now_str(),
            "status_dot_class": "dot-ok" if healthy else "dot-idle",
            "status_label": "Healthy · all systems nominal" if healthy else "Idle · waiting for first run",
            "cron_interval": settings.cron_interval_minutes,
            "last_run_relative": _humanize_age(last_run) if last_run else None,
            "last_run_iso": last_run.isoformat(timespec="seconds") if last_run else None,
            "processed_total": processed_total,
            "processed_today": processed_today,
            "manual_review_open": manual_review_open,
            "pipeline": [
                {
                    "title": "Poll Mercado Libre",
                    "body": "Every cron tick, fetch orders updated since the last successful run; filter to Flex via /shipments.logistic_type.",
                },
                {
                    "title": "Map + normalize",
                    "body": "Translate ML payload to a SimpliRoute visit; normalize AR addresses (abbreviations, accents, floor/depto).",
                },
                {
                    "title": "Geocode (cached)",
                    "body": f"Resolve coordinates via {settings.geocoder_backend.title()}; cache by SHA-256 of the normalized address.",
                },
                {
                    "title": "Two-stage write",
                    "body": "Claim a 'pending' row + COMMIT before the SimpliRoute POST; mark 'completed' only after the visit is created.",
                },
                {
                    "title": "Alert on failure",
                    "body": "Critical paths (OAuth refresh, SimpliRoute 5xx, watchdog) page Telegram with stable dedup keys.",
                },
            ],
            "defenses": [
                {"title": "UNIQUE(ml_order_id)", "body": "Database-level guarantee that one ML order maps to at most one row."},
                {"title": "Two-stage write", "body": "Claim before call, complete after success — crashes leave a row to retry, not a duplicate."},
                {"title": "Reference-based recovery", "body": "On retry, find_visit_by_reference looks up an existing SimpliRoute visit by ml_order_id and rebinds it instead of creating a new one."},
                {"title": "Visit reference", "body": "Every SimpliRoute visit carries the ML order id as its external reference, both for audit and recovery."},
                {"title": "Postgres advisory lock", "body": "pg_try_advisory_lock at the top of run_sync prevents two workers from racing on the same orders."},
            ],
        },
    )


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    settings = get_settings()
    db_ok = True
    try:
        session.execute(select(func.now()))
    except Exception:
        db_ok = False

    last_run = _last_successful_run(session)
    cron_dot, cron_label = _cron_indicator(last_run, settings.cron_watchdog_minutes)

    token = session.execute(
        select(OAuthToken).where(OAuthToken.provider == OAuthProvider.mercadolibre)
    ).scalar_one_or_none()
    token_dot, token_label = _token_indicator(token)

    runs = session.execute(
        select(CronRun).order_by(CronRun.id.desc()).limit(20)
    ).scalars().all()
    cron_runs = [
        {
            "started_at": _fmt(r.started_at),
            "status": r.status.value,
            "processed_count": r.processed_count,
            "skipped_count": r.skipped_count,
            "errors_count": r.errors_count,
            "notes": r.notes,
        }
        for r in runs
    ]

    alerts = session.execute(
        select(AlertDedup).order_by(AlertDedup.last_sent_at.desc()).limit(15)
    ).scalars().all()
    alert_rows = [
        {
            "last_sent_at": _fmt(a.last_sent_at),
            "last_level": a.last_level,
            "dedup_key": a.dedup_key,
            "last_title": a.last_title,
        }
        for a in alerts
    ]

    manual_review_open = session.execute(
        select(func.count(ManualReview.id)).where(
            ManualReview.resolved_at.is_(None)
        )
    ).scalar() or 0

    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "now": _now_str(),
            "db_ok": db_ok,
            "cron_dot_class": cron_dot,
            "cron_label": cron_label,
            "token_dot_class": token_dot,
            "token_label": token_label,
            "manual_review_open": manual_review_open,
            "cron_runs": cron_runs,
            "alerts": alert_rows,
        },
    )


# -- helpers --


def _last_successful_run(session: Session) -> datetime | None:
    row = session.execute(
        select(CronRun.finished_at)
        .where(CronRun.status == CronRunStatus.success)
        .order_by(CronRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.tzinfo is None:
        row = row.replace(tzinfo=timezone.utc)
    return row


def _cron_indicator(last_run: datetime | None, watchdog_min: int) -> tuple[str, str]:
    if last_run is None:
        return "dot-idle", "No runs yet"
    age_min = (datetime.now(timezone.utc) - last_run).total_seconds() / 60
    if age_min > watchdog_min:
        return "dot-err", f"Stale ({int(age_min)} min ago)"
    if age_min > watchdog_min / 2:
        return "dot-warn", f"Last {int(age_min)} min ago"
    return "dot-ok", f"Last {int(age_min)} min ago"


def _token_indicator(token: OAuthToken | None) -> tuple[str, str]:
    if token is None:
        return "dot-err", "Not authorized"
    expires = token.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delta = (expires - datetime.now(timezone.utc)).total_seconds() / 60
    if delta < 0:
        return "dot-warn", "Expired (will refresh on next call)"
    return "dot-ok", f"Valid for {int(delta)} min"


def _start_of_today() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _humanize_age(when: datetime) -> str:
    delta = (datetime.now(timezone.utc) - when).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
