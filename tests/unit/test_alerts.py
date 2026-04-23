from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.alerts import service as alerts
from app.alerts import telegram
from app.core.config import get_settings
from app.db.models import AlertDedup


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTBOT:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("ALERT_DEDUP_WINDOW_MINUTES", "15")
    yield
    get_settings.cache_clear()


def _telegram_url() -> str:
    return f"https://api.telegram.org/bot{get_settings().telegram_bot_token}/sendMessage"


# ---- Telegram client ----


@respx.mock
def test_telegram_post_message_happy_path():
    route = respx.post(_telegram_url()).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert telegram.post_message("Title", "Body", "error") is True
    assert route.call_count == 1
    request = route.calls.last.request
    body = request.content.decode()
    assert "123456" in body
    assert "Title" in body


@respx.mock
def test_telegram_post_message_swallows_http_error():
    respx.post(_telegram_url()).mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    assert telegram.post_message("Title", "Body", "error") is False


def test_telegram_returns_false_when_not_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    get_settings.cache_clear()
    assert telegram.post_message("t", "b", "error") is False


# ---- send_alert + dedup ----


@respx.mock
def test_send_alert_dedup_window_suppresses_repeat(session):
    respx.post(_telegram_url()).mock(return_value=httpx.Response(200, json={"ok": True}))

    first = alerts.send_alert(
        session, level="error", title="x", body="y", dedup_key="k1"
    )
    assert first is True

    second = alerts.send_alert(
        session, level="error", title="x", body="y", dedup_key="k1"
    )
    # Within 15 min window — suppressed.
    assert second is False

    rows = session.execute(select(AlertDedup).where(AlertDedup.dedup_key == "k1")).scalars().all()
    assert len(rows) == 1


@respx.mock
def test_send_alert_after_window_expires(session):
    route = respx.post(_telegram_url()).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    # Pre-insert a stale dedup row (30 min ago, outside the 15 min window).
    stale = datetime.now(timezone.utc) - timedelta(minutes=30)
    session.add(AlertDedup(dedup_key="k2", last_sent_at=stale, last_level="error"))
    session.commit()

    sent = alerts.send_alert(
        session, level="critical", title="x", body="y", dedup_key="k2"
    )
    assert sent is True
    assert route.call_count == 1

    refreshed = session.execute(
        select(AlertDedup).where(AlertDedup.dedup_key == "k2")
    ).scalar_one()
    # last_sent_at should have been advanced.
    last = refreshed.last_sent_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    assert last > stale


@respx.mock
def test_send_alert_different_keys_both_fire(session):
    route = respx.post(_telegram_url()).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    alerts.send_alert(session, level="error", title="a", body="b", dedup_key="key-a")
    alerts.send_alert(session, level="error", title="a", body="b", dedup_key="key-b")
    assert route.call_count == 2


# ---- Trigger helpers use stable dedup keys ----


def test_trigger_helpers_use_stable_keys(session):
    with patch("app.alerts.service.telegram.post_message", return_value=True) as mock_post:
        alerts.alert_ml_refresh_failed(session, "bad token")
        alerts.alert_simpliroute_permanent(session, "ML-1", "400 bad")
        alerts.alert_cron_watchdog(session, 60)

    assert mock_post.call_count == 3
    keys = {row.dedup_key for row in session.execute(select(AlertDedup)).scalars()}
    assert {"ml_refresh_failed", "simpliroute_permanent", "cron_watchdog"} <= keys


def test_manual_review_alert_keys_by_reason(session):
    """Different reasons -> different dedup keys so we see each root cause
    at least once per window, not just the first kind."""
    with patch("app.alerts.service.telegram.post_message", return_value=True):
        alerts.alert_manual_review(session, "1001", "missing_street_number")
        alerts.alert_manual_review(session, "1002", "geocode_failed")

    keys = {row.dedup_key for row in session.execute(select(AlertDedup)).scalars()}
    assert "manual_review:missing_street_number" in keys
    assert "manual_review:geocode_failed" in keys
