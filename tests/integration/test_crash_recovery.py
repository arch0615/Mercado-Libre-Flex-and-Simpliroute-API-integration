"""Crash-recovery path: simulate a process that created a SimpliRoute
visit but died before the DB UPDATE marked it completed. The next run
must recover the existing visit by reference instead of creating a
duplicate."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import OrderStatus, ProcessedOrder
from app.scheduler.job import run_sync

from .conftest import mk_ml_order, mk_ml_shipment


def test_pending_row_with_existing_sr_visit_is_recovered(
    session, env, seed_token, stub_external_apis
):
    """Scenario:
    - A previous run inserted `processed_orders(ml_order_id='7001', status=pending)`
      then crashed right after POSTing to SimpliRoute (visit id 42).
    - The next run sees the pending row, calls find_visit_by_reference,
      finds visit 42, binds it, and flips the row to completed.
    - NO new visit is created.
    """
    mock = stub_external_apis
    s = get_settings()

    # Seed: pending row with no SR visit_id yet.
    session.add(
        ProcessedOrder(
            ml_order_id="7001",
            status=OrderStatus.pending,
            processed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    order = mk_ml_order(7001)
    shipment = mk_ml_shipment(order["shipping"]["id"])
    mock.get(f"{s.ml_api_base}/orders/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [order],
                "paging": {"total": 1, "limit": 50, "offset": 0},
            },
        )
    )
    mock.get(f"{s.ml_api_base}/shipments/{shipment['id']}").mock(
        return_value=httpx.Response(200, json=shipment)
    )

    # find_visit_by_reference returns the visit created in the prior run.
    find_route = mock.get(f"{s.simpliroute_api_base}/v1/routes/visits/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": 42, "reference": "7001"}],
            },
        )
    )
    # create_visit MUST NOT be called on the recovery path.
    create_route = mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/")

    summary = run_sync(session)

    assert summary.processed == 1
    assert summary.errors == 0
    assert find_route.call_count == 1
    assert create_route.call_count == 0, "recovery must not POST a second visit"

    session.expire_all()
    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "7001")
    ).scalar_one()
    assert row.status == OrderStatus.completed
    assert row.simpliroute_visit_id == "42"


def test_pending_retry_without_existing_visit_creates_it(
    session, env, seed_token, stub_external_apis
):
    """Counterpart: pending row with no matching SR visit means the
    previous attempt failed before POST. Retry should CREATE the visit
    (not recover)."""
    mock = stub_external_apis
    s = get_settings()

    session.add(
        ProcessedOrder(
            ml_order_id="7002",
            status=OrderStatus.failed,
            retries=1,
            error="previous transient error",
            processed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    order = mk_ml_order(7002)
    shipment = mk_ml_shipment(order["shipping"]["id"])
    mock.get(f"{s.ml_api_base}/orders/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [order],
                "paging": {"total": 1, "limit": 50, "offset": 0},
            },
        )
    )
    mock.get(f"{s.ml_api_base}/shipments/{shipment['id']}").mock(
        return_value=httpx.Response(200, json=shipment)
    )
    # find returns no existing visit.
    mock.get(f"{s.simpliroute_api_base}/v1/routes/visits/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/").mock(
        return_value=httpx.Response(201, json={"id": 4242, "reference": "7002"})
    )

    summary = run_sync(session)
    assert summary.processed == 1

    session.expire_all()
    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "7002")
    ).scalar_one()
    assert row.status == OrderStatus.completed
    assert row.simpliroute_visit_id == "4242"
    # retries counter should NOT decrement; error cleared on success.
    assert row.error is None
