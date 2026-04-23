"""Acceptance-criterion test: re-run the cron, zero duplicates.

This directly matches the contract's acceptance line:
  "procesamiento correcto de al menos 3 pedidos Flex reales pasando a
   SimpliRoute sin duplicados ni errores, incluyendo una reejecución
   del cron para verificar idempotencia."
"""
from __future__ import annotations

import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import CronRun, OrderStatus, ProcessedOrder
from app.scheduler.job import run_sync

from .conftest import mk_ml_order, mk_ml_shipment


def test_three_runs_create_three_visits_once(
    session, env, seed_token, stub_external_apis
):
    mock = stub_external_apis
    s = get_settings()

    orders = [mk_ml_order(5001), mk_ml_order(5002), mk_ml_order(5003)]
    shipments = [mk_ml_shipment(o["shipping"]["id"]) for o in orders]

    # ML returns the same 3 orders on every poll (same result page every call).
    mock.get(f"{s.ml_api_base}/orders/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": orders,
                "paging": {"total": 3, "limit": 50, "offset": 0},
            },
        )
    )
    for shp in shipments:
        mock.get(f"{s.ml_api_base}/shipments/{shp['id']}").mock(
            return_value=httpx.Response(200, json=shp)
        )

    # SimpliRoute: every create call increments and returns a new id; if a
    # second call arrives for the same order we WOULD see a second visit
    # id here, which the processor must prevent.
    sr_create_route = mock.post(f"{s.simpliroute_api_base}/v1/routes/visits/")
    counter = {"n": 9000}

    def _create(_request):
        counter["n"] += 1
        return httpx.Response(
            201, json={"id": counter["n"], "reference": str(counter["n"])}
        )

    sr_create_route.side_effect = _create

    # --- first run: 3 completed
    s1 = run_sync(session)
    assert s1.processed == 3
    assert sr_create_route.call_count == 3

    # --- second run: same orders, everything already completed
    s2 = run_sync(session)
    assert s2.processed == 0
    assert s2.skipped == 3
    assert sr_create_route.call_count == 3  # NO new SR calls

    # --- third run: still zero duplicates
    s3 = run_sync(session)
    assert s3.skipped == 3
    assert sr_create_route.call_count == 3

    # DB invariants
    rows = session.execute(select(ProcessedOrder)).scalars().all()
    assert len(rows) == 3
    for r in rows:
        assert r.status == OrderStatus.completed
        assert r.simpliroute_visit_id is not None

    # Count grouped by ml_order_id should be 1 per key (UNIQUE enforced).
    counts = session.execute(
        select(ProcessedOrder.ml_order_id, func.count(ProcessedOrder.id))
        .group_by(ProcessedOrder.ml_order_id)
    ).all()
    assert all(count == 1 for _, count in counts)

    # Cron runs recorded: 3 success rows total.
    runs = session.execute(
        select(CronRun).order_by(CronRun.id.asc())
    ).scalars().all()
    assert len(runs) == 3
