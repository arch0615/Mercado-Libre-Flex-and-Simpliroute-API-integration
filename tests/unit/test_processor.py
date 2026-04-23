from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    ManualReview,
    OrderStatus,
    ProcessedOrder,
)
from app.geocoder.base import GeocodeResult
from app.ml.orders import FetchedOrder
from app.scheduler.processor import (
    REASON_GEOCODE_FAILED,
    REASON_GEOCODE_LOW_CONFIDENCE,
    REASON_SIMPLIROUTE_PERMANENT,
    Outcome,
    process_order,
)
from app.simpliroute.client import (
    PermanentSimpliRouteError,
    TransientSimpliRouteError,
)


@pytest.fixture(autouse=True)
def _config_with_sr_token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SIMPLIROUTE_TOKEN", "test-token")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fake")
    monkeypatch.setenv("GEOCODER_MIN_CONFIDENCE", "0.7")
    yield
    get_settings.cache_clear()


def _order(order_id: int = 1001, *, with_coords: bool = True) -> dict:
    o = {
        "id": order_id,
        "buyer": {"id": 1, "first_name": "Juan", "last_name": "Pérez"},
        "shipping": {"id": 5001},
        "order_items": [{"item": {"title": "Producto"}, "quantity": 1}],
    }
    return o


def _shipment(*, with_coords: bool = True, address_ok: bool = True) -> dict:
    addr = {
        "street_name": "Av. Corrientes",
        "street_number": "1234" if address_ok else "",
        "receiver_name": "Juan Pérez",
        "receiver_phone": "011-44445555",
        "city": {"name": "CABA"},
        "state": {"name": "Buenos Aires"},
        "country": {"name": "Argentina"},
    }
    if with_coords:
        addr["latitude"] = -34.6
        addr["longitude"] = -58.4
    return {"id": 5001, "logistic_type": "self_service", "receiver_address": addr}


def _fetched(order=None, shipment=None) -> FetchedOrder:
    return FetchedOrder(
        order=deepcopy(order or _order()),
        shipment=deepcopy(shipment or _shipment()),
    )


def _mock_sr_client(create_response=None, find_response=None, create_side_effect=None):
    sr = MagicMock()
    if create_side_effect is not None:
        sr.create_visit.side_effect = create_side_effect
    else:
        sr.create_visit.return_value = create_response or {"id": 9001}
    sr.find_visit_by_reference.return_value = find_response
    return sr


# ---- Happy path ----


def test_happy_path_new_order_creates_visit_and_marks_completed(session):
    sr = _mock_sr_client(create_response={"id": 9001})
    result = process_order(session, _fetched(), sr)
    assert result.outcome == Outcome.completed
    assert result.visit_id == "9001"
    sr.create_visit.assert_called_once()
    # find_visit_by_reference must NOT be called on the new-order path
    # (it would be a wasted SimpliRoute call on the happy path).
    sr.find_visit_by_reference.assert_not_called()

    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
    ).scalar_one()
    assert row.status == OrderStatus.completed
    assert row.simpliroute_visit_id == "9001"
    assert row.completed_at is not None


# ---- Dedup: UNIQUE constraint in action ----


def test_already_completed_order_is_skipped_without_calling_simpliroute(session):
    # Seed a completed row.
    session.add(
        ProcessedOrder(
            ml_order_id="1001",
            status=OrderStatus.completed,
            simpliroute_visit_id="9000",
            processed_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    sr = _mock_sr_client()
    result = process_order(session, _fetched(), sr)
    assert result.outcome == Outcome.skipped_duplicate
    sr.create_visit.assert_not_called()
    sr.find_visit_by_reference.assert_not_called()


# ---- Crash recovery: retry finds existing visit ----


def test_retry_recovers_existing_simpliroute_visit(session):
    """If a previous run crashed after creating the visit but before the
    final UPDATE, the pending row is retried; find_visit_by_reference
    must discover the existing visit and bind it instead of creating a
    duplicate."""
    session.add(
        ProcessedOrder(
            ml_order_id="1001",
            status=OrderStatus.pending,
            processed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    sr = _mock_sr_client(find_response={"id": 7777, "reference": "1001"})
    result = process_order(session, _fetched(), sr)

    assert result.outcome == Outcome.recovered
    assert result.visit_id == "7777"
    sr.find_visit_by_reference.assert_called_once_with("1001")
    sr.create_visit.assert_not_called()

    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
    ).scalar_one()
    assert row.status == OrderStatus.completed
    assert row.simpliroute_visit_id == "7777"


def test_retry_without_existing_visit_creates_new_one(session):
    session.add(
        ProcessedOrder(
            ml_order_id="1001",
            status=OrderStatus.failed,
            retries=1,
            error="old error",
            processed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    sr = _mock_sr_client(find_response=None, create_response={"id": 9002})
    result = process_order(session, _fetched(), sr)

    assert result.outcome == Outcome.completed
    sr.find_visit_by_reference.assert_called_once()
    sr.create_visit.assert_called_once()

    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
    ).scalar_one()
    assert row.status == OrderStatus.completed
    assert row.error is None  # cleared on success


# ---- Error handling ----


def test_permanent_simpliroute_error_goes_to_manual_review(session):
    sr = _mock_sr_client(
        create_side_effect=PermanentSimpliRouteError(400, "bad data", "u")
    )
    result = process_order(session, _fetched(), sr)

    assert result.outcome == Outcome.permanent_failed
    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
    ).scalar_one()
    assert row.status == OrderStatus.failed
    assert row.retries == 1

    review = session.execute(
        select(ManualReview).where(ManualReview.ml_order_id == "1001")
    ).scalar_one()
    assert review.reason == REASON_SIMPLIROUTE_PERMANENT


def test_transient_simpliroute_error_leaves_row_for_retry(session):
    sr = _mock_sr_client(
        create_side_effect=TransientSimpliRouteError(503, "flaky", "u")
    )
    result = process_order(session, _fetched(), sr)

    assert result.outcome == Outcome.transient_failed
    row = session.execute(
        select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
    ).scalar_one()
    assert row.status == OrderStatus.failed
    assert row.retries == 1
    # No manual_review for transient — it's still eligible for auto-retry.
    assert (
        session.execute(
            select(ManualReview).where(ManualReview.ml_order_id == "1001")
        ).scalar_one_or_none()
        is None
    )


# ---- Missing data -> manual_review ----


def test_missing_street_number_goes_to_manual_review(session):
    fetched = _fetched(shipment=_shipment(address_ok=False))
    sr = _mock_sr_client()
    result = process_order(session, fetched, sr)

    assert result.outcome == Outcome.manual_review
    assert result.reason == "missing_street_number"
    sr.create_visit.assert_not_called()
    # No processed_orders row — we never attempted to send.
    assert (
        session.execute(
            select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
        ).scalar_one_or_none()
        is None
    )
    review = session.execute(
        select(ManualReview).where(ManualReview.ml_order_id == "1001")
    ).scalar_one()
    assert review.reason == "missing_street_number"


# ---- Geocoder integration ----


def test_missing_coordinates_triggers_geocoder(session):
    # Strip coordinates from the fixture.
    shipment = _shipment(with_coords=False)
    fetched = _fetched(shipment=shipment)
    sr = _mock_sr_client()

    geocoder = MagicMock()
    geocoder.name = "mockgeo"
    geocoder.geocode.return_value = GeocodeResult(
        lat=-34.6, lng=-58.4, confidence=0.95, backend="mockgeo"
    )

    result = process_order(session, fetched, sr, geocoder=geocoder)
    assert result.outcome == Outcome.completed
    geocoder.geocode.assert_called_once()
    # Payload sent to SimpliRoute must have the new coordinates.
    called_with = sr.create_visit.call_args.args[0]
    assert called_with.latitude == -34.6
    assert called_with.longitude == -58.4


def test_geocoder_miss_routes_to_manual_review(session):
    fetched = _fetched(shipment=_shipment(with_coords=False))
    sr = _mock_sr_client()
    geocoder = MagicMock()
    geocoder.name = "mockgeo"
    geocoder.geocode.return_value = None

    result = process_order(session, fetched, sr, geocoder=geocoder)
    assert result.outcome == Outcome.manual_review
    assert result.reason == REASON_GEOCODE_FAILED
    sr.create_visit.assert_not_called()


def test_low_confidence_geocode_routes_to_manual_review(session):
    fetched = _fetched(shipment=_shipment(with_coords=False))
    sr = _mock_sr_client()
    geocoder = MagicMock()
    geocoder.name = "mockgeo"
    geocoder.geocode.return_value = GeocodeResult(
        lat=-34.6, lng=-58.4, confidence=0.4, backend="mockgeo"
    )

    result = process_order(session, fetched, sr, geocoder=geocoder)
    assert result.outcome == Outcome.manual_review
    assert result.reason == REASON_GEOCODE_LOW_CONFIDENCE
    sr.create_visit.assert_not_called()


# ---- Idempotency: calling process_order twice on same order ----


def test_double_invocation_does_not_duplicate(session):
    """The acceptance criterion: running the processor twice on the same
    order must not create two visits."""
    sr = _mock_sr_client(create_response={"id": 9001})
    r1 = process_order(session, _fetched(), sr)
    r2 = process_order(session, _fetched(), sr)

    assert r1.outcome == Outcome.completed
    assert r2.outcome == Outcome.skipped_duplicate
    assert sr.create_visit.call_count == 1  # only the first invocation hit SR

    rows = (
        session.execute(
            select(ProcessedOrder).where(ProcessedOrder.ml_order_id == "1001")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # UNIQUE enforced
