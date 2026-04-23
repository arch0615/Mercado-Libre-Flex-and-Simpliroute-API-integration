"""Alert service with DB-backed dedup.

Every alert call specifies a `dedup_key`. If the same key was sent within
`ALERT_DEDUP_WINDOW_MINUTES` (default 15) the alert is suppressed; the
last-sent timestamp is refreshed anyway so sustained incidents don't
reset the window repeatedly.

Also exposes named trigger helpers (`alert_ml_refresh_failed`, etc.) so
call sites use stable dedup keys without having to invent them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import telegram
from app.core.config import get_settings
from app.core.logging import logger
from app.db.models import AlertDedup

AlertLevel = Literal["warning", "error", "critical"]


def send_alert(
    session: Session,
    *,
    level: AlertLevel,
    title: str,
    body: str,
    dedup_key: str,
) -> bool:
    """Send an alert unless a recent one with the same dedup_key was already sent.

    Returns True if the alert was actually sent to Telegram.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.alert_dedup_window_minutes)

    existing = session.execute(
        select(AlertDedup).where(AlertDedup.dedup_key == dedup_key)
    ).scalar_one_or_none()

    if existing is not None:
        last = existing.last_sent_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last > cutoff:
            logger.debug(
                "alert_deduped",
                dedup_key=dedup_key,
                window_min=settings.alert_dedup_window_minutes,
            )
            return False

    sent = telegram.post_message(title, body, level)

    if existing is None:
        session.add(
            AlertDedup(
                dedup_key=dedup_key,
                last_sent_at=now,
                last_title=title[:500],
                last_level=level,
            )
        )
    else:
        existing.last_sent_at = now
        existing.last_title = title[:500]
        existing.last_level = level
    session.commit()

    logger.info(
        "alert_sent" if sent else "alert_delivery_failed",
        dedup_key=dedup_key,
        level=level,
        title=title,
    )
    return sent


# -- Named trigger helpers (stable dedup keys) --


def alert_ml_refresh_failed(session: Session, detail: str) -> None:
    send_alert(
        session,
        level="critical",
        title="ML OAuth refresh failed",
        body=f"get_valid_access_token() could not refresh the ML token.\n\nDetail: {detail}\n\nAction: re-authorize via /oauth/authorize.",
        dedup_key="ml_refresh_failed",
    )


def alert_simpliroute_permanent(session: Session, ml_order_id: str, detail: str) -> None:
    send_alert(
        session,
        level="error",
        title=f"SimpliRoute 4xx on order {ml_order_id}",
        body=f"Order sent to manual_review.\n\nDetail: {detail[:500]}",
        dedup_key="simpliroute_permanent",
    )


def alert_simpliroute_transient_exhausted(
    session: Session, ml_order_id: str, detail: str
) -> None:
    send_alert(
        session,
        level="critical",
        title="SimpliRoute repeated 5xx / timeout",
        body=f"Order {ml_order_id} left pending after all retries.\n\nDetail: {detail[:500]}",
        dedup_key="simpliroute_transient",
    )


def alert_manual_review(session: Session, ml_order_id: str, reason: str) -> None:
    send_alert(
        session,
        level="warning",
        title=f"Order {ml_order_id} needs manual review",
        body=f"Reason: {reason}",
        dedup_key=f"manual_review:{reason}",
    )


def alert_cron_watchdog(session: Session, minutes_stale: int) -> None:
    send_alert(
        session,
        level="critical",
        title=f"No successful cron run in {minutes_stale} minutes",
        body="The scheduler has not posted a successful cron_run in the watchdog window. Check Railway logs.",
        dedup_key="cron_watchdog",
    )


def alert_refresh_token_aging(session: Session, months_old: float) -> None:
    send_alert(
        session,
        level="warning",
        title=f"ML refresh token is {months_old:.1f} months old",
        body="Forcing a preventive exchange now. If this alert repeats, the refresh chain may be broken — re-authorize via /oauth/authorize.",
        dedup_key="refresh_token_aging",
    )


def alert_cron_failed(session: Session, detail: str) -> None:
    send_alert(
        session,
        level="critical",
        title="Cron run failed unexpectedly",
        body=f"run_sync raised an unhandled exception.\n\nDetail: {detail[:500]}",
        dedup_key="cron_run_failed",
    )
