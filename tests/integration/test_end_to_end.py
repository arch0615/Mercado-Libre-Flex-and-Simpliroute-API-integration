"""End-to-end integration tests for the full sync pipeline.

These tests exercise `run_sync` with real processor, orchestrator, geocoder
cache, two-stage write, and advisory-lock-noop-on-SQLite — only the outbound
HTTP to ML / SimpliRoute / Google / Telegram is stubbed via respx.

Goal: catch wiring bugs that unit tests would miss (module boundaries,
serialization, ordering, DB state after multiple steps).
"""
from __future__ import annotations

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    CronRun,
    CronRunStatus,
    OrderStatus,
    ProcessedOrder,
)
from app.scheduler.job import run_sync

from .conftest import mk_ml_order, mk_ml_shipment


def _stub_ml_orders(mock, *orders):
    page = {
        "results": orders,
        "paging": {"total": len(orders), "limit": 50, "offset": 0},
    }
    s = get_settings()
    mock.get(f"{s.ml_api_base}/orders/search").mock(
        return_value=httpx.Response(200, json=page)
    )


def _stub_ml_shipment(mock, shipment):
    s = get_settings()
    mock.get(f"{s.ml_api_base}/shipments/{shipment['id']}").mock(
        return_value=httpx.Response(200, json=shipment)
    )


def _stub_simpliroute_create(mock, visit_ids):
    """visit_ids: list of ids to return in order (one per call)."""
    s = get_settings()
    responses = [
        httpx.Response(201, json={"id": vid, "reference": str(vid)})
        for vid in visit_ids
    ]
    mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/").side_effect = responses


def test_end_to_end_three_real_flex_orders(
    session, env, seed_token, stub_external_apis
):
    """The acceptance-criterion happy path: 3 Flex orders -> 3 SR visits
    -> 3 completed rows, zero errors, zero manual review."""
    mock = stub_external_apis

    orders = [mk_ml_order(1001), mk_ml_order(1002), mk_ml_order(1003)]
    _stub_ml_orders(mock, *orders)
    for o in orders:
        _stub_ml_shipment(mock, mk_ml_shipment(o["shipping"]["id"]))
    _stub_simpliroute_create(mock, [9001, 9002, 9003])

    summary = run_sync(session)

    assert summary.status == CronRunStatus.success
    assert summary.processed == 3
    assert summary.skipped == 0
    assert summary.manual_review == 0
    assert summary.errors == 0

    rows = {
        r.ml_order_id: r
        for r in session.execute(select(ProcessedOrder)).scalars()
    }
    assert set(rows.keys()) == {"1001", "1002", "1003"}
    for r in rows.values():
        assert r.status == OrderStatus.completed
        assert r.simpliroute_visit_id is not None
        assert r.completed_at is not None

    cron = session.execute(
        select(CronRun).order_by(CronRun.id.desc()).limit(1)
    ).scalar_one()
    assert cron.status == CronRunStatus.success
    assert cron.processed_count == 3
    assert cron.errors_count == 0


def test_non_flex_shipments_are_filtered_out(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis

    orders = [mk_ml_order(2001), mk_ml_order(2002)]
    _stub_ml_orders(mock, *orders)
    # 2001 is Flex, 2002 is cross_docking and must be dropped.
    _stub_ml_shipment(mock, mk_ml_shipment(orders[0]["shipping"]["id"]))
    _stub_ml_shipment(
        mock,
        mk_ml_shipment(
            orders[1]["shipping"]["id"], logistic_type="cross_docking"
        ),
    )
    _stub_simpliroute_create(mock, [9101])

    summary = run_sync(session)

    assert summary.processed == 1
    assert summary.errors == 0
    rows = session.execute(select(ProcessedOrder)).scalars().all()
    assert {r.ml_order_id for r in rows} == {"2001"}


def test_ml_returns_no_orders_still_records_success(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    _stub_ml_orders(mock)  # empty
    summary = run_sync(session)
    assert summary.status == CronRunStatus.success
    assert summary.processed == 0
