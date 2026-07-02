"""On-demand paths (webhook + QR scan) exercised end-to-end.

Like the cron integration test, these run the real `process_order` against
SQLite with only the external HTTP calls stubbed. The key contract is
idempotency: resolving the same order twice must never create two visits.
"""
from __future__ import annotations

import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import OrderStatus, ProcessedOrder
from app.scheduler.ondemand import process_by_order_id, process_by_shipment_id

from .conftest import mk_ml_order, mk_ml_shipment


def _stub_order_and_shipment(mock, s, order_id, shipment_id, *, logistic="self_service"):
    order = mk_ml_order(order_id, shipment_id=shipment_id)
    shipment = mk_ml_shipment(shipment_id, logistic_type=logistic)
    shipment["order_id"] = order_id
    mock.get(f"{s.ml_api_base}/orders/{order_id}").mock(
        return_value=httpx.Response(200, json=order)
    )
    mock.get(f"{s.ml_api_base}/shipments/{shipment_id}").mock(
        return_value=httpx.Response(200, json=shipment)
    )
    return order, shipment


def _stub_sr_create(mock, s):
    route = mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/")
    counter = {"n": 9000}

    def _create(_request):
        counter["n"] += 1
        return httpx.Response(
            201, json={"id": counter["n"], "reference": str(counter["n"])}
        )

    route.side_effect = _create
    return route


def test_scan_by_shipment_creates_then_is_idempotent(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    s = get_settings()
    _stub_order_and_shipment(mock, s, 5001, 6001)
    sr_route = _stub_sr_create(mock, s)

    r1 = process_by_shipment_id(session, "6001")
    assert r1.found is True
    assert r1.ml_order_id == "5001"
    assert r1.outcome == "completed"
    assert sr_route.call_count == 1

    # Scanning the same label again must not duplicate.
    r2 = process_by_shipment_id(session, "6001")
    assert r2.outcome == "skipped_duplicate"
    assert sr_route.call_count == 1

    count = session.execute(select(func.count(ProcessedOrder.id))).scalar()
    assert count == 1
    row = session.execute(select(ProcessedOrder)).scalar_one()
    assert row.status == OrderStatus.completed


def test_process_by_order_id_creates_visit(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    s = get_settings()
    _stub_order_and_shipment(mock, s, 5002, 6002)
    _stub_sr_create(mock, s)

    r = process_by_order_id(session, "5002")
    assert r.found is True
    assert r.ml_order_id == "5002"
    assert r.outcome == "completed"


def test_scan_non_flex_shipment_is_not_processed(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    s = get_settings()
    _stub_order_and_shipment(mock, s, 5003, 6003, logistic="cross_docking")

    r = process_by_shipment_id(session, "6003")
    assert r.found is False
    assert r.reason == "shipment_not_flex_or_no_order"
    count = session.execute(select(func.count(ProcessedOrder.id))).scalar()
    assert count == 0
