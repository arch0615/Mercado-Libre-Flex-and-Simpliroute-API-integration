from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.db.models import (
    CronRun,
    CronRunStatus,
    OAuthProvider,
    OAuthToken,
)
from app.ml.oauth import OAuthError, TokenPayload
from app.scheduler.watchdog import (
    TOKEN_REFRESH_THRESHOLD,
    check_cron_health,
    check_token_health,
)


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CRON_WATCHDOG_MINUTES", "45")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTBOT:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    yield
    get_settings.cache_clear()


# ---- Cron watchdog ----


def test_watchdog_returns_unhealthy_without_alert_when_no_runs_yet(session):
    result = check_cron_health(session)
    assert result.healthy is False
    assert result.minutes_since_last_success is None
    assert result.alerted is False


def test_watchdog_healthy_when_last_run_fresh(session):
    finished = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        CronRun(
            started_at=finished - timedelta(seconds=10),
            finished_at=finished,
            status=CronRunStatus.success,
        )
    )
    session.commit()

    with patch("app.scheduler.watchdog.alerts.alert_cron_watchdog") as alert:
        result = check_cron_health(session)

    assert result.healthy is True
    assert result.alerted is False
    alert.assert_not_called()


def test_watchdog_alerts_when_stale(session):
    finished = datetime.now(timezone.utc) - timedelta(minutes=90)
    session.add(
        CronRun(
            started_at=finished - timedelta(seconds=10),
            finished_at=finished,
            status=CronRunStatus.success,
        )
    )
    session.commit()

    with patch("app.scheduler.watchdog.alerts.alert_cron_watchdog") as alert:
        result = check_cron_health(session)

    assert result.healthy is False
    assert result.alerted is True
    assert result.minutes_since_last_success is not None
    assert result.minutes_since_last_success >= 89
    alert.assert_called_once()


def test_watchdog_ignores_failed_runs(session):
    # Only successful runs reset the watchdog.
    session.add(
        CronRun(
            started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            finished_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            status=CronRunStatus.failed,
        )
    )
    session.commit()
    result = check_cron_health(session)
    assert result.healthy is False


# ---- Token health ----


def test_token_health_noop_when_no_token(session):
    result = check_token_health(session)
    assert result.refreshed is False
    assert result.age_days is None


def test_token_health_noop_when_token_is_fresh(session):
    session.add(
        OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token="AT",
            refresh_token="RT",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=5),
        )
    )
    session.commit()

    with patch("app.scheduler.watchdog.refresh_access_token") as refresh_mock:
        result = check_token_health(session)

    assert result.refreshed is False
    assert result.alerted is False
    refresh_mock.assert_not_called()


def test_token_health_forces_refresh_when_aging(session):
    aging_updated_at = datetime.now(timezone.utc) - TOKEN_REFRESH_THRESHOLD - timedelta(days=5)
    token = OAuthToken(
        provider=OAuthProvider.mercadolibre,
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=5),
    )
    session.add(token)
    session.commit()
    # Back-date updated_at manually (auto-timestamped on insert).
    session.execute(
        OAuthToken.__table__.update()
        .where(OAuthToken.id == token.id)
        .values(updated_at=aging_updated_at)
    )
    session.commit()
    session.expire_all()  # drop identity-map cache so the backdate is visible

    new_payload = TokenPayload(
        access_token="NEW-AT",
        refresh_token="NEW-RT",
        expires_in=21600,
        user_id="123",
        scope=None,
    )
    with patch(
        "app.scheduler.watchdog.refresh_access_token",
        return_value=new_payload,
    ) as refresh_mock, patch(
        "app.scheduler.watchdog.alerts.alert_refresh_token_aging"
    ) as aging_alert:
        result = check_token_health(session)

    assert result.refreshed is True
    assert result.alerted is True
    aging_alert.assert_called_once()
    refresh_mock.assert_called_once_with("RT")

    session.expire_all()
    refreshed = session.get(OAuthToken, token.id)
    assert refreshed.refresh_token == "NEW-RT"


def test_token_health_alerts_and_returns_unrefreshed_on_oauth_error(session):
    aging_updated_at = datetime.now(timezone.utc) - TOKEN_REFRESH_THRESHOLD - timedelta(days=2)
    token = OAuthToken(
        provider=OAuthProvider.mercadolibre,
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=5),
    )
    session.add(token)
    session.commit()
    session.execute(
        OAuthToken.__table__.update()
        .where(OAuthToken.id == token.id)
        .values(updated_at=aging_updated_at)
    )
    session.commit()
    session.expire_all()

    with patch(
        "app.scheduler.watchdog.refresh_access_token",
        side_effect=OAuthError("invalid_grant"),
    ), patch(
        "app.scheduler.watchdog.alerts.alert_refresh_token_aging"
    ), patch(
        "app.scheduler.watchdog.alerts.alert_ml_refresh_failed"
    ) as failed_alert:
        result = check_token_health(session)

    assert result.refreshed is False
    assert result.alerted is True
    failed_alert.assert_called_once()
