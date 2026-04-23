from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.transform import (
    MISSING_PHONE_PLACEHOLDER,
    REASON_EMPTY_ITEMS,
    REASON_MISSING_ADDRESS,
    REASON_MISSING_STREET_NUMBER,
    ManualReview,
    VisitPayload,
    map_order_to_visit,
)
from app.ml.orders import FetchedOrder


def _order(**overrides) -> dict:
    base = {
        "id": 200000001,
        "buyer": {
            "id": 111,
            "first_name": "Juan",
            "last_name": "Pérez",
            "phone": {"area_code": "011", "number": "44445555"},
        },
        "shipping": {"id": 400001},
        "order_items": [
            {
                "item": {"title": "Auriculares Bluetooth"},
                "quantity": 1,
            },
            {
                "item": {"title": "Cable USB C"},
                "quantity": 2,
            },
        ],
    }
    base.update(overrides)
    return base


def _shipment(**overrides) -> dict:
    base = {
        "id": 400001,
        "logistic_type": "self_service",
        "receiver_address": {
            "address_line": "Av. Corrientes 1234",
            "street_name": "Av. Corrientes",
            "street_number": "1234",
            "comment": "Piso 5 Depto B",
            "receiver_name": "Juan Pérez",
            "receiver_phone": "011-44445555",
            "zip_code": "C1043AAZ",
            "city": {"name": "CABA"},
            "state": {"name": "Buenos Aires"},
            "country": {"name": "Argentina"},
            "latitude": -34.6037,
            "longitude": -58.3816,
        },
    }
    base.update(overrides)
    return base


def _fetched(order=None, shipment=None, buyer_profile=None) -> FetchedOrder:
    return FetchedOrder(
        order=deepcopy(order or _order()),
        shipment=deepcopy(shipment or _shipment()),
        buyer_profile=buyer_profile,
    )


def test_happy_path_returns_visit_payload():
    result = map_order_to_visit(_fetched())
    assert isinstance(result, VisitPayload)
    assert result.reference == "200000001"
    assert result.contact_name == "Juan Pérez"
    assert "Avenida Corrientes 1234" in result.address
    assert "piso 5" in result.address
    assert "CABA" in result.address
    assert result.contact_phone == "011-44445555"
    assert result.latitude == -34.6037
    assert result.longitude == -58.3816
    # Reference must be present for SimpliRoute traceability.
    assert str(200000001) in result.reference


def test_items_summary_in_notes():
    result = map_order_to_visit(_fetched())
    assert isinstance(result, VisitPayload)
    assert "1x Auriculares Bluetooth" in result.notes
    assert "2x Cable USB C" in result.notes


def test_missing_phone_uses_placeholder_and_flag():
    shipment = _shipment()
    shipment["receiver_address"]["receiver_phone"] = None
    order = _order()
    order["buyer"]["phone"] = None
    result = map_order_to_visit(_fetched(order=order, shipment=shipment))
    assert isinstance(result, VisitPayload)
    assert result.contact_phone == MISSING_PHONE_PLACEHOLDER
    assert "[sin tel]" in result.notes


def test_phone_from_buyer_when_address_phone_missing():
    shipment = _shipment()
    shipment["receiver_address"]["receiver_phone"] = ""
    result = map_order_to_visit(_fetched(shipment=shipment))
    assert isinstance(result, VisitPayload)
    # Falls back to buyer.phone -> "011 44445555"
    assert "44445555" in result.contact_phone


def test_missing_street_number_goes_to_manual_review():
    shipment = _shipment()
    shipment["receiver_address"]["street_number"] = ""
    result = map_order_to_visit(_fetched(shipment=shipment))
    assert isinstance(result, ManualReview)
    assert result.reason == REASON_MISSING_STREET_NUMBER


def test_missing_street_name_goes_to_manual_review():
    shipment = _shipment()
    shipment["receiver_address"]["street_name"] = ""
    shipment["receiver_address"]["address_line"] = ""
    result = map_order_to_visit(_fetched(shipment=shipment))
    assert isinstance(result, ManualReview)
    assert result.reason == REASON_MISSING_ADDRESS


def test_empty_items_goes_to_manual_review():
    order = _order(order_items=[])
    result = map_order_to_visit(_fetched(order=order))
    assert isinstance(result, ManualReview)
    assert result.reason == REASON_EMPTY_ITEMS


def test_special_characters_are_normalized():
    shipment = _shipment()
    # Compatibility form of "ñ" (decomposed n + combining tilde).
    shipment["receiver_address"]["street_name"] = "Nuñez"
    shipment["receiver_address"]["receiver_name"] = "María González"
    result = map_order_to_visit(_fetched(shipment=shipment))
    assert isinstance(result, VisitPayload)
    # NFKC preserves precomposed ñ and accented vowels.
    assert "Nuñez" in result.address
    assert result.contact_name == "María González"


def test_more_than_five_items_truncates_with_count():
    items = [
        {"item": {"title": f"Producto {i}"}, "quantity": 1} for i in range(8)
    ]
    order = _order(order_items=items)
    result = map_order_to_visit(_fetched(order=order))
    assert isinstance(result, VisitPayload)
    # Shows first 5 plus a "+3 más" tail.
    assert "Producto 0" in result.notes
    assert "Producto 4" in result.notes
    assert "Producto 5" not in result.notes
    assert "+3 más" in result.notes


def test_receiver_name_falls_back_to_buyer_name():
    shipment = _shipment()
    shipment["receiver_address"]["receiver_name"] = ""
    result = map_order_to_visit(_fetched(shipment=shipment))
    assert isinstance(result, VisitPayload)
    assert result.contact_name == "Juan Pérez"


def test_comment_appears_as_reference_note():
    result = map_order_to_visit(_fetched())
    assert isinstance(result, VisitPayload)
    assert "Ref:" in result.notes


@pytest.mark.parametrize(
    "missing_field",
    ["street_name", "address_line"],
)
def test_manual_review_never_raises(missing_field):
    # Even with ill-formed payload, we must return a ManualReview, never raise.
    shipment = _shipment()
    shipment["receiver_address"][missing_field] = None
    result = map_order_to_visit(_fetched(shipment=shipment))
    assert isinstance(result, (VisitPayload, ManualReview))
