from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import CronRun, CronRunStatus, ManualReview, OAuthProvider, OAuthToken
from app.ml.api import MLClient
from app.ml.orders import (
    DEFAULT_LOOKBACK,
    PAGE_SIZE,
    REASON_NO_SHIPMENT,
    fetch_new_orders,
    fetch_order_by_id,
    fetch_order_by_shipment,
    get_cursor,
)


@pytest.fixture(autouse=True)
def _settings_with_seller(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ML_SELLER_ID", "99999")
    yield
    get_settings.cache_clear()


def _seed_valid_token(session):
    session.add(
        OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token="VALID-AT",
            refresh_token="VALID-RT",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=5),
        )
    )
    session.commit()


def _orders_url() -> str:
    return f"{get_settings().ml_api_base}/orders/search"


def _shipment_url(shipment_id: int) -> str:
    return f"{get_settings().ml_api_base}/shipments/{shipment_id}"


def _mk_order(order_id: int, shipment_id: int) -> dict:
    return {
        "id": order_id,
        "status": "paid",
        "shipping": {"id": shipment_id},
    }


def _mk_shipment(shipment_id: int, logistic_type: str = "self_service") -> dict:
    return {"id": shipment_id, "logistic_type": logistic_type}


def test_get_cursor_returns_default_lookback_when_no_runs(session):
    cursor = get_cursor(session)
    expected = datetime.now(timezone.utc) - DEFAULT_LOOKBACK
    # Allow a few seconds of slack.
    assert abs((cursor - expected).total_seconds()) < 5


def test_get_cursor_uses_last_successful_run_with_overlap(session):
    ref = datetime.now(timezone.utc) - timedelta(hours=2)
    session.add(
        CronRun(
            started_at=ref,
            finished_at=ref + timedelta(seconds=30),
            status=CronRunStatus.success,
        )
    )
    session.commit()

    cursor = get_cursor(session)
    # Cursor should be ref - CURSOR_OVERLAP (15 minutes).
    delta = ref - cursor
    assert timedelta(minutes=14) < delta < timedelta(minutes=16)


def test_get_cursor_ignores_non_success_runs(session):
    now = datetime.now(timezone.utc)
    session.add(
        CronRun(
            started_at=now - timedelta(minutes=30),
            status=CronRunStatus.failed,
        )
    )
    session.commit()
    cursor = get_cursor(session)
    # Should still fall back to DEFAULT_LOOKBACK.
    expected = now - DEFAULT_LOOKBACK
    assert abs((cursor - expected).total_seconds()) < 5


@respx.mock
def test_fetch_new_orders_filters_non_flex_shipments(session):
    _seed_valid_token(session)
    orders_page = {
        "results": [
            _mk_order(1001, 5001),
            _mk_order(1002, 5002),
            _mk_order(1003, 5003),
        ],
        "paging": {"total": 3, "limit": PAGE_SIZE, "offset": 0},
    }
    respx.get(_orders_url()).mock(return_value=httpx.Response(200, json=orders_page))
    respx.get(_shipment_url(5001)).mock(
        return_value=httpx.Response(200, json=_mk_shipment(5001, "self_service"))
    )
    # Not Flex — should be filtered out.
    respx.get(_shipment_url(5002)).mock(
        return_value=httpx.Response(200, json=_mk_shipment(5002, "cross_docking"))
    )
    respx.get(_shipment_url(5003)).mock(
        return_value=httpx.Response(200, json=_mk_shipment(5003, "self_service"))
    )

    with MLClient(session) as client:
        summary = fetch_new_orders(
            session,
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            client=client,
        )

    assert len(summary.fetched) == 2
    assert {fo.ml_order_id for fo in summary.fetched} == {"1001", "1003"}
    assert summary.skipped_non_flex == 1


@respx.mock
def test_fetch_new_orders_paginates(session):
    _seed_valid_token(session)

    page_one = {
        "results": [_mk_order(2000 + i, 6000 + i) for i in range(PAGE_SIZE)],
        "paging": {"total": PAGE_SIZE + 1, "limit": PAGE_SIZE, "offset": 0},
    }
    page_two = {
        "results": [_mk_order(3000, 7000)],
        "paging": {"total": PAGE_SIZE + 1, "limit": PAGE_SIZE, "offset": PAGE_SIZE},
    }

    # Respond with page_one on first call, page_two on second.
    orders_route = respx.get(_orders_url())
    orders_route.side_effect = [
        httpx.Response(200, json=page_one),
        httpx.Response(200, json=page_two),
    ]
    # Any shipment responds as Flex.
    respx.get(url__regex=r".*/shipments/\d+").mock(
        return_value=httpx.Response(200, json={"logistic_type": "self_service"})
    )

    with MLClient(session) as client:
        summary = fetch_new_orders(
            session,
            since=datetime.now(timezone.utc) - timedelta(hours=2),
            client=client,
        )

    assert len(summary.fetched) == PAGE_SIZE + 1
    assert orders_route.call_count == 2


@respx.mock
def test_fetch_new_orders_skips_orders_without_shipment(session):
    _seed_valid_token(session)
    orders_page = {
        "results": [
            # No shipping.id -> must be skipped.
            {"id": 9001, "shipping": {}},
            _mk_order(9002, 8002),
        ],
        "paging": {"total": 2, "limit": PAGE_SIZE, "offset": 0},
    }
    respx.get(_orders_url()).mock(return_value=httpx.Response(200, json=orders_page))
    respx.get(_shipment_url(8002)).mock(
        return_value=httpx.Response(200, json=_mk_shipment(8002))
    )

    with MLClient(session) as client:
        summary = fetch_new_orders(
            session,
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            client=client,
        )

    assert summary.skipped_no_shipment == 1
    assert [fo.ml_order_id for fo in summary.fetched] == ["9002"]


@respx.mock
def test_no_shipment_order_is_flagged_for_manual_review(session):
    """A dropped (no-shipment) order must leave a visible trace, not vanish."""
    _seed_valid_token(session)
    orders_page = {
        "results": [
            {"id": 9001, "shipping": {}, "pack_id": 7777},  # dropped
        ],
        "paging": {"total": 1, "limit": PAGE_SIZE, "offset": 0},
    }
    respx.get(_orders_url()).mock(return_value=httpx.Response(200, json=orders_page))

    with MLClient(session) as client:
        summary = fetch_new_orders(
            session,
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            client=client,
        )

    assert summary.skipped_no_shipment == 1
    review = session.execute(
        select(ManualReview).where(ManualReview.ml_order_id == "9001")
    ).scalar_one()
    assert review.reason == REASON_NO_SHIPMENT


@respx.mock
def test_fetch_order_by_id_returns_flex_order(session):
    _seed_valid_token(session)
    respx.get(f"{get_settings().ml_api_base}/orders/1001").mock(
        return_value=httpx.Response(200, json=_mk_order(1001, 5001))
    )
    respx.get(_shipment_url(5001)).mock(
        return_value=httpx.Response(200, json=_mk_shipment(5001, "self_service"))
    )
    with MLClient(session) as client:
        fetched = fetch_order_by_id(client, "1001")
    assert fetched is not None
    assert fetched.ml_order_id == "1001"


@respx.mock
def test_fetch_order_by_id_skips_non_flex(session):
    _seed_valid_token(session)
    respx.get(f"{get_settings().ml_api_base}/orders/1002").mock(
        return_value=httpx.Response(200, json=_mk_order(1002, 5002))
    )
    respx.get(_shipment_url(5002)).mock(
        return_value=httpx.Response(200, json=_mk_shipment(5002, "cross_docking"))
    )
    with MLClient(session) as client:
        assert fetch_order_by_id(client, "1002") is None


@respx.mock
def test_fetch_order_by_shipment_resolves_order(session):
    _seed_valid_token(session)
    shipment = _mk_shipment(5003, "self_service")
    shipment["order_id"] = 1003
    respx.get(_shipment_url(5003)).mock(
        return_value=httpx.Response(200, json=shipment)
    )
    respx.get(f"{get_settings().ml_api_base}/orders/1003").mock(
        return_value=httpx.Response(200, json=_mk_order(1003, 5003))
    )
    with MLClient(session) as client:
        fetched = fetch_order_by_shipment(client, "5003")
    assert fetched is not None
    assert fetched.ml_order_id == "1003"


@respx.mock
def test_fetch_order_by_shipment_skips_non_flex(session):
    _seed_valid_token(session)
    shipment = _mk_shipment(5004, "cross_docking")
    shipment["order_id"] = 1004
    respx.get(_shipment_url(5004)).mock(
        return_value=httpx.Response(200, json=shipment)
    )
    with MLClient(session) as client:
        assert fetch_order_by_shipment(client, "5004") is None
