"""Fixtures shared by end-to-end integration tests.

These tests DO NOT mock the processor or job modules — they exercise the
real orchestration against a SQLite-in-memory DB, with only the external
HTTP dependencies (ML, SimpliRoute, Telegram, Google) stubbed via respx.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.db.models import OAuthProvider, OAuthToken


@pytest.fixture
def env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ML_CLIENT_ID", "cid")
    monkeypatch.setenv("ML_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ML_REDIRECT_URI", "http://localhost/oauth/callback")
    monkeypatch.setenv("ML_SELLER_ID", "99999")
    monkeypatch.setenv("ML_TRIGGER_STATUS", "paid")
    monkeypatch.setenv("SIMPLIROUTE_TOKEN", "sr-token")
    monkeypatch.setenv("SIMPLIROUTE_MAX_RETRIES", "2")
    monkeypatch.setenv("SIMPLIROUTE_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("GEOCODER_BACKEND", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "gk")
    monkeypatch.setenv("GEOCODER_MIN_CONFIDENCE", "0.7")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTBOT:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("INTERNAL_SECRET", "s3cr3t")
    yield
    get_settings.cache_clear()


@pytest.fixture
def seed_token(session):
    """Seed a valid ML OAuth token so API calls work without a refresh hop."""
    session.add(
        OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token="AT",
            refresh_token="RT",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        )
    )
    session.commit()


def mk_ml_order(order_id: int, *, shipment_id: int | None = None) -> dict:
    return {
        "id": order_id,
        "status": "paid",
        "shipping": {"id": shipment_id or (order_id + 5000)},
        "buyer": {"first_name": "Cliente", "last_name": f"Nro{order_id}"},
        "order_items": [
            {"item": {"title": f"Producto {order_id}"}, "quantity": 1}
        ],
    }


def mk_ml_shipment(
    shipment_id: int,
    *,
    logistic_type: str = "self_service",
    with_coords: bool = True,
) -> dict:
    addr = {
        "street_name": "Av. Corrientes",
        "street_number": "1234",
        "receiver_name": "Cliente",
        "receiver_phone": "011-44445555",
        "city": {"name": "CABA"},
        "state": {"name": "Buenos Aires"},
        "country": {"name": "Argentina"},
    }
    if with_coords:
        addr["latitude"] = -34.6037
        addr["longitude"] = -58.3816
    return {"id": shipment_id, "logistic_type": logistic_type, "receiver_address": addr}


@pytest.fixture
def stub_external_apis():
    """Context-manager-like fixture that stubs all external HTTP calls.

    The yielded dict lets tests assert call counts for specific routes.
    """
    with respx.mock(assert_all_called=False) as mock:
        s = get_settings()
        # Telegram: always OK (alerts are best-effort).
        mock.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))

        yield mock
