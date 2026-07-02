from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_db


@pytest.fixture
def client(session, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ML_SELLER_ID", "99999")

    from app.main import app

    def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers():
    return {}


def test_notification_order_topic_schedules_processing(client):
    with patch("app.ml.webhooks.process_by_order_id") as m:
        resp = client.post(
            "/ml/notifications",
            json={"topic": "orders_v2", "resource": "/orders/5001", "user_id": 99999},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["resource_id"] == "5001"
    m.assert_called_once()
    assert m.call_args.args[1] == "5001"


def test_notification_shipment_topic_schedules_processing(client):
    with patch("app.ml.webhooks.process_by_shipment_id") as m:
        resp = client.post(
            "/ml/notifications",
            json={"topic": "shipments", "resource": "/shipments/6001", "user_id": 99999},
        )
    assert resp.status_code == 200
    assert resp.json()["resource_id"] == "6001"
    m.assert_called_once()
    assert m.call_args.args[1] == "6001"


def test_notification_ignores_other_seller(client):
    with patch("app.ml.webhooks.process_by_order_id") as m:
        resp = client.post(
            "/ml/notifications",
            json={"topic": "orders_v2", "resource": "/orders/5001", "user_id": 11111},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    m.assert_not_called()


def test_notification_skips_unknown_topic(client):
    with patch("app.ml.webhooks.process_by_order_id") as m_order, patch(
        "app.ml.webhooks.process_by_shipment_id"
    ) as m_ship:
        resp = client.post(
            "/ml/notifications",
            json={"topic": "questions", "resource": "/questions/1", "user_id": 99999},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    m_order.assert_not_called()
    m_ship.assert_not_called()
