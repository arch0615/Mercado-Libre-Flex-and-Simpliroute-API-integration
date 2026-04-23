from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    CronRun,
    CronRunStatus,
    OAuthProvider,
    OAuthToken,
    OrderStatus,
    ProcessedOrder,
)
from app.ml.api import MLClient
from app.ml.orders import FetchedOrder, FetchSummary
from app.scheduler.job import run_sync
from app.scheduler.lock import LockNotAcquired


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ML_SELLER_ID", "99999")
    monkeypatch.setenv("SIMPLIROUTE_TOKEN", "test-token")
    monkeypatch.setenv("INTERNAL_SECRET", "s3cr3t")
    yield
    get_settings.cache_clear()


def _seed_valid_token(session):
    session.add(
        OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token="AT",
            refresh_token="RT",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=5),
        )
    )
    session.commit()


def _order_and_shipment(order_id: int = 2001):
    order = {
        "id": order_id,
        "buyer": {"first_name": "Ana", "last_name": "García"},
        "shipping": {"id": 6000 + order_id},
        "order_items": [{"item": {"title": "Thing"}, "quantity": 1}],
    }
    shipment = {
        "id": 6000 + order_id,
        "logistic_type": "self_service",
        "receiver_address": {
            "street_name": "Av. Corrientes",
            "street_number": "1234",
            "receiver_name": "Ana García",
            "receiver_phone": "011-44445555",
            "city": {"name": "CABA"},
            "state": {"name": "Buenos Aires"},
            "country": {"name": "Argentina"},
            "latitude": -34.6,
            "longitude": -58.4,
        },
    }
    return order, shipment


def _fetched_orders(count: int):
    out = []
    for i in range(count):
        order, shipment = _order_and_shipment(order_id=2001 + i)
        out.append(FetchedOrder(order=order, shipment=shipment))
    return out


def _mock_sr(create_results=None):
    sr = MagicMock()
    sr.find_visit_by_reference.return_value = None
    if create_results is not None:
        sr.create_visit.side_effect = create_results
    else:
        counter = {"n": 0}

        def _create(_payload):
            counter["n"] += 1
            return {"id": 9000 + counter["n"]}

        sr.create_visit.side_effect = _create
    return sr


def test_run_sync_success_records_cron_run_and_counters(session):
    _seed_valid_token(session)
    sr = _mock_sr()
    ml = MagicMock(spec=MLClient)

    with patch(
        "app.scheduler.job.fetch_new_orders",
        return_value=FetchSummary(fetched=_fetched_orders(3)),
    ):
        summary = run_sync(session, ml_client=ml, simpliroute_client=sr)

    assert summary.status == CronRunStatus.success
    assert summary.processed == 3
    assert summary.skipped == 0
    assert summary.errors == 0

    cron_run = session.execute(
        select(CronRun).order_by(CronRun.id.desc()).limit(1)
    ).scalar_one()
    assert cron_run.status == CronRunStatus.success
    assert cron_run.processed_count == 3
    assert cron_run.finished_at is not None


def test_run_sync_skipped_when_lock_held(session):
    _seed_valid_token(session)
    sr = _mock_sr()
    ml = MagicMock(spec=MLClient)

    with patch(
        "app.scheduler.job.advisory_lock",
        side_effect=LockNotAcquired("held"),
    ):
        summary = run_sync(session, ml_client=ml, simpliroute_client=sr)

    assert summary.status == CronRunStatus.skipped
    cron_run = session.execute(
        select(CronRun).order_by(CronRun.id.desc()).limit(1)
    ).scalar_one()
    assert cron_run.status == CronRunStatus.skipped
    assert cron_run.notes is not None


def test_run_sync_mixed_outcomes_counted_correctly(session):
    _seed_valid_token(session)

    orders = _fetched_orders(3)
    # Pre-seed order 2002 as completed so it goes to 'skipped_duplicate'.
    session.add(
        ProcessedOrder(
            ml_order_id="2002",
            status=OrderStatus.completed,
            processed_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            simpliroute_visit_id="OLD",
        )
    )
    session.commit()

    sr = _mock_sr()
    ml = MagicMock(spec=MLClient)

    with patch(
        "app.scheduler.job.fetch_new_orders",
        return_value=FetchSummary(fetched=orders),
    ):
        summary = run_sync(session, ml_client=ml, simpliroute_client=sr)

    # 2001 + 2003 should complete (2), 2002 should skip (1), no errors.
    assert summary.processed == 2
    assert summary.skipped == 1
    assert summary.errors == 0
    assert sr.create_visit.call_count == 2


def test_run_sync_raises_and_marks_cron_run_failed(session):
    _seed_valid_token(session)
    sr = _mock_sr()
    ml = MagicMock(spec=MLClient)

    with patch(
        "app.scheduler.job.fetch_new_orders",
        side_effect=RuntimeError("ml api exploded"),
    ):
        with pytest.raises(RuntimeError, match="ml api exploded"):
            run_sync(session, ml_client=ml, simpliroute_client=sr)

    cron_run = session.execute(
        select(CronRun).order_by(CronRun.id.desc()).limit(1)
    ).scalar_one()
    assert cron_run.status == CronRunStatus.failed
    assert "ml api exploded" in (cron_run.notes or "")
