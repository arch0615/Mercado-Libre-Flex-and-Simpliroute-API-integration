"""Parse the QR printed on a Mercado Libre Flex shipping label.

The Flex label QR usually encodes a small JSON object that carries the
shipment id, e.g. ``{"id": 40592476937, "sender_id": 123, "hash_code": ...}``.
Some scanner apps hand us the raw shipment id instead, and occasionally a
URL. This helper accepts any of those forms and returns the shipment id as a
string, or None when nothing usable is found.
"""
from __future__ import annotations

import json
import re

# Keys that, in observed Flex QR payloads, hold the shipment id.
_ID_KEYS = ("id", "shipment_id", "shipping_id")


def parse_flex_qr(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    # 1) Raw numeric shipment id — the simplest scanner output.
    if text.isdigit():
        return text

    # 2) JSON payload — the common Flex label form.
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        for key in _ID_KEYS:
            value = data.get(key)
            if value is not None and str(value).isdigit():
                return str(value)

    # 3) Fallback: the first long run of digits (e.g. embedded in a URL).
    match = re.search(r"\d{6,}", text)
    return match.group(0) if match else None
