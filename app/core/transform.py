"""Map Mercado Libre fetched orders to SimpliRoute visit payloads.

The boundary contract:
- Input: `FetchedOrder` (order + shipment, optionally enriched buyer).
- Output: `TransformResult`, which is either a ready-to-send `VisitPayload`
  or a `ManualReview(reason, detail)` tuple.

We never raise for bad data — bad data must be routed to `manual_review` so
the scheduler can log + alert without failing the whole cron run.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.core.address import NormalizedAddress, normalize_address
from app.ml.orders import FetchedOrder

MISSING_PHONE_PLACEHOLDER = "0000000000"


@dataclass
class VisitPayload:
    """Shape consumed by SimpliRoute's POST /v1/routes/visits."""

    reference: str  # ml_order_id — used as external_id for traceability
    title: str
    address: str
    latitude: float | None
    longitude: float | None
    contact_name: str
    contact_phone: str
    notes: str
    window_start: str | None = None
    window_end: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ManualReview:
    reason: str
    detail: str


TransformResult = VisitPayload | ManualReview


# Reason codes stored in manual_review.reason (max 128 chars).
REASON_MISSING_ADDRESS = "missing_address"
REASON_MISSING_STREET_NUMBER = "missing_street_number"
REASON_MISSING_RECEIVER = "missing_receiver_name"
REASON_EMPTY_ITEMS = "empty_items"


def map_order_to_visit(fetched: FetchedOrder) -> TransformResult:
    order = fetched.order
    shipment = fetched.shipment or {}

    address_source = (
        shipment.get("receiver_address")
        or (order.get("shipping") or {}).get("receiver_address")
    )
    normalized = normalize_address(address_source)
    if normalized is None or not normalized.street_name:
        return ManualReview(
            REASON_MISSING_ADDRESS, "No usable street_name in receiver_address"
        )
    if not normalized.street_number:
        return ManualReview(
            REASON_MISSING_STREET_NUMBER,
            f"Street '{normalized.street_name}' has no number",
        )

    receiver_name = _receiver_name(fetched, address_source or {})
    if not receiver_name:
        return ManualReview(REASON_MISSING_RECEIVER, "No receiver_name on order")

    items = order.get("order_items") or []
    if not items:
        return ManualReview(REASON_EMPTY_ITEMS, "Order has no order_items")

    phone, phone_missing = _phone_or_placeholder(fetched, address_source or {})

    notes_parts = [_items_summary(items)]
    if phone_missing:
        notes_parts.append("[sin tel]")
    comment = _clean(address_source.get("comment") if address_source else None)
    if comment:
        notes_parts.append(f"Ref: {comment}")

    window_start, window_end = _shipment_window(shipment)

    return VisitPayload(
        reference=str(order["id"]),
        title=_title_for_visit(receiver_name, order["id"]),
        address=normalized.full_line,
        latitude=normalized.latitude,
        longitude=normalized.longitude,
        contact_name=receiver_name,
        contact_phone=phone,
        notes=" | ".join(p for p in notes_parts if p),
        window_start=window_start,
        window_end=window_end,
        extra={"normalized_address": normalized_as_dict(normalized)},
    )


def normalized_as_dict(n: NormalizedAddress) -> dict:
    return {
        "street_name": n.street_name,
        "street_number": n.street_number,
        "floor": n.floor,
        "apartment": n.apartment,
        "city": n.city,
        "state": n.state,
        "zip_code": n.zip_code,
        "country": n.country,
    }


def _receiver_name(fetched: FetchedOrder, address: dict) -> str:
    for source in (
        address.get("receiver_name"),
        (fetched.shipment.get("receiver_address") or {}).get("receiver_name"),
        _buyer_full_name(fetched.order.get("buyer") or {}),
        (fetched.buyer_profile or {}).get("nickname"),
    ):
        cleaned = _clean(source)
        if cleaned:
            return cleaned
    return ""


def _buyer_full_name(buyer: dict) -> str | None:
    first = _clean(buyer.get("first_name"))
    last = _clean(buyer.get("last_name"))
    if first or last:
        return " ".join(p for p in (first, last) if p)
    return None


def _phone_or_placeholder(fetched: FetchedOrder, address: dict) -> tuple[str, bool]:
    candidates = [
        address.get("receiver_phone"),
        (fetched.order.get("buyer") or {}).get("phone"),
        (fetched.buyer_profile or {}).get("phone"),
    ]
    for candidate in candidates:
        phone = _extract_phone(candidate)
        if phone:
            return phone, False
    return MISSING_PHONE_PLACEHOLDER, True


def _extract_phone(value) -> str | None:  # noqa: ANN001
    if not value:
        return None
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        area = _clean(value.get("area_code"))
        number = _clean(value.get("number"))
        if area or number:
            return " ".join(p for p in (area, number) if p)
    return None


def _items_summary(items: list[dict]) -> str:
    parts: list[str] = []
    for item in items[:5]:
        product = item.get("item") or {}
        title = _clean(product.get("title")) or "item"
        qty = item.get("quantity") or 1
        parts.append(f"{qty}x {title}")
    if len(items) > 5:
        parts.append(f"(+{len(items) - 5} más)")
    return "; ".join(parts)


def _title_for_visit(receiver_name: str, order_id: Any) -> str:
    return f"{receiver_name} — ML#{order_id}"


def _shipment_window(shipment: dict) -> tuple[str | None, str | None]:
    """Return (start, end) ISO strings if the shipment has a declared window."""
    window = (
        shipment.get("shipping_option") or {}
    ).get("estimated_delivery_time") or {}
    start = window.get("date")
    # ML sometimes exposes 'pay_before' and 'estimated_delivery' windows separately.
    offset = window.get("offset")
    if start and offset is not None:
        # Defer formatting to SimpliRoute's strict format at send time.
        return start, None
    return None, None


def _clean(value) -> str | None:  # noqa: ANN001
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = unicodedata.normalize("NFKC", value).strip()
    return value or None
