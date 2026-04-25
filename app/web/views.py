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
    icon_clock = '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>'
    icon_map = '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 20l-5.447-2.724A1 1 0 013 16.382V5.618a1 1 0 011.447-.894L9 7m0 13l6-3m-6 3V7m6 10l4.553 2.276A1 1 0 0021 18.382V7.618a1 1 0 00-.553-.894L15 4m0 13V4m0 0L9 7"/></svg>'
    icon_pin = '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M17.657 16.657L13.414 20.9a2 2 0 01-2.828 0l-4.244-4.243a8 8 0 1111.314 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/></svg>'
    icon_lock = '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>'
    icon_bell = '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/></svg>'

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
                {"icon": icon_clock, "title": "Poll Mercado Libre", "body": "Every cron tick, fetch orders updated since the last successful run; filter to Flex via shipment.logistic_type."},
                {"icon": icon_map,   "title": "Map + normalize",    "body": "Translate ML payload to a SimpliRoute visit; normalize AR addresses (abbreviations, accents, floor / depto)."},
                {"icon": icon_pin,   "title": "Geocode (cached)",   "body": f"Resolve coordinates via {settings.geocoder_backend.title()}; cache by SHA-256 of the normalized address."},
                {"icon": icon_lock,  "title": "Two-stage write",    "body": "Claim a 'pending' row + COMMIT before SimpliRoute; mark 'completed' only after the visit is created."},
                {"icon": icon_bell,  "title": "Alert on failure",   "body": "OAuth refresh, SimpliRoute 5xx, watchdog — all page Telegram with stable dedup keys."},
            ],
            "defenses": [
                {"tag": "DB",      "title": "UNIQUE(ml_order_id)",      "body": "Database-level guarantee that one ML order maps to at most one row in processed_orders."},
                {"tag": "Logic",   "title": "Two-stage write",          "body": "Claim before call, complete after success — crashes leave a row to retry, not a duplicate visit."},
                {"tag": "Recover", "title": "Reference-based recovery", "body": "On retry, find_visit_by_reference looks up an existing SimpliRoute visit by ml_order_id and rebinds it instead of creating a new one."},
                {"tag": "Trace",   "title": "Visit reference",          "body": "Every SimpliRoute visit carries the ML order id as its external reference — for audit, and to enable the recovery path above."},
                {"tag": "Lock",    "title": "Postgres advisory lock",   "body": "pg_try_advisory_lock at the top of run_sync prevents two workers from racing on the same orders."},
            ],
        },
    )


@router.get("/docs", response_class=HTMLResponse, name="api_reference")
def api_reference(request: Request) -> HTMLResponse:
    """Curated, themed API reference. Native Swagger UI lives at /swagger."""
    settings = get_settings()
    base_url = str(request.base_url).rstrip("/")
    public_url = base_url

    groups = [
        {
            "tag": "Operator surfaces",
            "tag_subtitle": "Pages a human will open in a browser. No auth required.",
            "color": "emerald",
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/",
                    "summary": "Landing page",
                    "description": "Service overview, live status pill, anti-duplicate guarantees.",
                    "auth": None,
                    "response_kind": "html",
                    "example_curl": f"curl {public_url}/",
                    "example_response": "<!doctype html>...",
                },
                {
                    "method": "GET",
                    "path": "/status",
                    "summary": "Operator status",
                    "description": "Read-only view: DB, cron, ML token, recent runs and alerts. Auto-refreshes every 30s.",
                    "auth": None,
                    "response_kind": "html",
                    "example_curl": f"curl {public_url}/status",
                    "example_response": "<!doctype html>...",
                },
            ],
        },
        {
            "tag": "OAuth",
            "tag_subtitle": "Mercado Libre authorization flow. Browser-driven.",
            "color": "cyan",
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/oauth/authorize",
                    "summary": "Start OAuth flow",
                    "description": "Redirects to ML's authorization page. After the seller approves, ML calls back to /oauth/callback.",
                    "auth": None,
                    "response_kind": "redirect",
                    "params": [{"name": "state", "in": "query", "required": False, "type": "string", "desc": "Optional opaque value echoed back in the callback."}],
                    "example_curl": f"open {public_url}/oauth/authorize",
                    "example_response": "302 Found\\nLocation: https://auth.mercadolibre.com.ar/authorization?...",
                },
                {
                    "method": "GET",
                    "path": "/oauth/callback",
                    "summary": "OAuth redirect target",
                    "description": "ML calls this with ?code=... after the user authorizes. Exchanges the code for an access + refresh token pair and persists them. Returns an HTML success or error page.",
                    "auth": None,
                    "response_kind": "html",
                    "params": [
                        {"name": "code",  "in": "query", "required": True,  "type": "string", "desc": "Single-use authorization code from ML."},
                        {"name": "error", "in": "query", "required": False, "type": "string", "desc": "Error code if the user denied the request."},
                    ],
                    "example_curl": f"# called by ML, not directly\\n# {public_url}/oauth/callback?code=TG-abc123",
                    "example_response": "200 OK · oauth_success.html",
                },
            ],
        },
        {
            "tag": "Internal · cron",
            "tag_subtitle": "Triggered by Railway cron jobs. Require X-Internal-Secret header.",
            "color": "amber",
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/internal/run",
                    "summary": "Run the sync job",
                    "description": "Acquires the advisory lock, fetches new Flex orders since the last successful run, processes each through the two-stage write into SimpliRoute, finalizes a cron_runs row.",
                    "auth": "X-Internal-Secret",
                    "response_kind": "json",
                    "example_curl": f"curl -X POST {public_url}/internal/run \\\\\n  -H \"X-Internal-Secret: $INTERNAL_SECRET\"",
                    "example_response": '{\n  "cron_run_id": 42,\n  "status": "success",\n  "processed": 3,\n  "skipped": 0,\n  "manual_review": 0,\n  "errors": 0\n}',
                },
                {
                    "method": "POST",
                    "path": "/internal/watchdog",
                    "summary": "Cron health watchdog",
                    "description": f"Alerts via Telegram if no cron_runs row with status=success has been written in the last {settings.cron_watchdog_minutes} minutes. Run on its own schedule (every 10 min) so a wedged main job doesn't silence itself.",
                    "auth": "X-Internal-Secret",
                    "response_kind": "json",
                    "example_curl": f"curl -X POST {public_url}/internal/watchdog \\\\\n  -H \"X-Internal-Secret: $INTERNAL_SECRET\"",
                    "example_response": '{\n  "healthy": true,\n  "minutes_since_last_success": 12,\n  "alerted": false\n}',
                },
                {
                    "method": "POST",
                    "path": "/internal/token-health",
                    "summary": "Preventive token refresh",
                    "description": "Forces a refresh_access_token exchange when the current refresh_token is older than 5 months. ML refresh tokens die at 6 months — this leaves ~1 month of safety margin. Run weekly.",
                    "auth": "X-Internal-Secret",
                    "response_kind": "json",
                    "example_curl": f"curl -X POST {public_url}/internal/token-health \\\\\n  -H \"X-Internal-Secret: $INTERNAL_SECRET\"",
                    "example_response": '{\n  "refreshed": false,\n  "age_days": 21,\n  "alerted": false\n}',
                },
            ],
        },
        {
            "tag": "Health",
            "tag_subtitle": "Liveness probe. No auth.",
            "color": "slate",
            "endpoints": [
                {
                    "method": "GET",
                    "path": "/health",
                    "summary": "Liveness + DB probe",
                    "description": "Returns service + DB status and the timestamp of the last successful cron run. Used by Railway healthchecks.",
                    "auth": None,
                    "response_kind": "json",
                    "example_curl": f"curl {public_url}/health",
                    "example_response": '{\n  "status": "ok",\n  "db": "ok",\n  "last_cron_run": "2026-04-23T12:34:56+00:00",\n  "last_cron_age_seconds": 145\n}',
                },
            ],
        },
    ]

    return templates.TemplateResponse(
        request,
        "api.html",
        {
            "now": _now_str(),
            "base_url": public_url,
            "groups": groups,
            "endpoint_count": sum(len(g["endpoints"]) for g in groups),
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
