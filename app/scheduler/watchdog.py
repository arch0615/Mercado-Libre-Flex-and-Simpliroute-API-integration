"""Out-of-band health checks fired by separate Railway cron jobs.

- `check_cron_health(session)`: alerts if no successful cron_runs row
  has been written in the configured watchdog window. Caught by a
  separate cron so a wedged main job doesn't silence itself.
- `check_token_health(session)`: forces a preventive refresh when the
  current refresh_token is older than 5 months. ML refresh tokens die
  at 6 months, so we act with a comfortable safety margin.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import service as alerts
from app.core.config import get_settings
from app.core.logging import logger
from app.db.models import CronRun, CronRunStatus, OAuthProvider, OAuthToken
from app.ml.oauth import OAuthError, persist_token, refresh_access_token

# Preventive refresh kicks in this far before the 6-month hard deadline.
TOKEN_REFRESH_THRESHOLD = timedelta(days=150)  # ~5 months


@dataclass
class WatchdogResult:
    healthy: bool
    minutes_since_last_success: int | None
    alerted: bool


@dataclass
class TokenHealthResult:
    refreshed: bool
    age_days: int | None
    alerted: bool


def check_cron_health(session: Session) -> WatchdogResult:
    settings = get_settings()
    last = session.execute(
        select(CronRun)
        .where(CronRun.status == CronRunStatus.success)
        .order_by(CronRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if last is None or last.finished_at is None:
        # First-run situation: don't alert; there's nothing to compare to.
        return WatchdogResult(healthy=False, minutes_since_last_success=None, alerted=False)

    finished = last.finished_at
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - finished
    age_min = int(age.total_seconds() // 60)

    if age > timedelta(minutes=settings.cron_watchdog_minutes):
        logger.warning("cron_watchdog_stale", age_minutes=age_min)
        alerts.alert_cron_watchdog(session, age_min)
        return WatchdogResult(healthy=False, minutes_since_last_success=age_min, alerted=True)

    return WatchdogResult(healthy=True, minutes_since_last_success=age_min, alerted=False)


def check_token_health(session: Session) -> TokenHealthResult:
    token = session.execute(
        select(OAuthToken).where(OAuthToken.provider == OAuthProvider.mercadolibre)
    ).scalar_one_or_none()

    if token is None:
        return TokenHealthResult(refreshed=False, age_days=None, alerted=False)

    updated = token.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)

    age = datetime.now(timezone.utc) - updated
    age_days = age.days
    if age < TOKEN_REFRESH_THRESHOLD:
        return TokenHealthResult(refreshed=False, age_days=age_days, alerted=False)

    # Token is aging. Force a preventive exchange so ML rotates us a fresh pair.
    months_old = age.days / 30.44
    alerts.alert_refresh_token_aging(session, months_old)

    try:
        payload = refresh_access_token(token.refresh_token)
    except OAuthError as exc:
        alerts.alert_ml_refresh_failed(session, str(exc))
        logger.exception("token_health_refresh_failed")
        return TokenHealthResult(refreshed=False, age_days=age_days, alerted=True)

    persist_token(session, payload)
    session.commit()
    logger.info("token_health_refreshed", age_days=age_days)
    return TokenHealthResult(refreshed=True, age_days=age_days, alerted=True)
