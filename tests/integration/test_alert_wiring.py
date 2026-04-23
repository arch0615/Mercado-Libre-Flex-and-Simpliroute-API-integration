"""End-to-end alert wiring: failure paths trigger Telegram + DB side-effects."""
from __future__ import annotations

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    AlertDedup,
    ManualReview,
    OrderStatus,
    ProcessedOrder,
)
from app.scheduler.job import run_sync

from .conftest import mk_ml_order, mk_ml_shipment


def test_simpliroute_permanent_4xx_routes_to_manual_review_and_alerts(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    s = get_settings()

    order = mk_ml_order(3001)
    shipment = mk_ml_shipment(order["shipping"]["id"])
    mock.get(f"{s.ml_api_base}/orders/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [order], "paging": {"total": 1, "limit": 50, "offset": 0}},
        )
    )
    mock.get(f"{s.ml_api_base}/shipments/{shipment['id']}").mock(
        return_value=httpx.Response(200, json=shipment)
    )
    mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/").mock(
        return_value=httpx.Response(400, text="address rejected")
    )
    telegram_route = mock.post(
        f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    )

    summary = run_sync(session)

    assert summary.errors == 1
    assert summary.processed == 0

    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "3001")
    ).scalar_one()
    assert row.status == OrderStatus.failed
    assert row.retries == 1

    review = session.execute(
        select(ManualReview).where(ManualReview.ml_order_id == "3001")
    ).scalar_one()
    assert review.reason == "simpliroute_permanent_error"

    # Telegram was called at least once for the permanent alert.
    assert telegram_route.call_count >= 1
    # Dedup row recorded under the stable key.
    keys = {r.dedup_key for r in session.execute(select(AlertDedup)).scalars()}
    assert "simpliroute_permanent" in keys


def test_missing_street_number_routes_to_manual_review_and_alerts(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    s = get_settings()

    order = mk_ml_order(3002)
    shipment = mk_ml_shipment(order["shipping"]["id"])
    # Corrupt the address.
    shipment["receiver_address"]["street_number"] = ""

    mock.get(f"{s.ml_api_base}/orders/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [order], "paging": {"total": 1, "limit": 50, "offset": 0}},
        )
    )
    mock.get(f"{s.ml_api_base}/shipments/{shipment['id']}").mock(
        return_value=httpx.Response(200, json=shipment)
    )
    create_route = mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/")
    telegram_route = mock.post(
        f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    )

    summary = run_sync(session)

    assert summary.manual_review == 1
    assert summary.processed == 0
    # Bad-data path must NOT hit SimpliRoute.
    assert create_route.call_count == 0
    # Manual-review alert sent (keyed by reason).
    assert telegram_route.call_count >= 1
    keys = {r.dedup_key for r in session.execute(select(AlertDedup)).scalars()}
    assert "manual_review:missing_street_number" in keys
