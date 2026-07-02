from __future__ import annotations

import json

import pytest

from app.ml.flex_label import parse_flex_qr


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("40592476937", "40592476937"),
        ("  40592476937  ", "40592476937"),
        (json.dumps({"id": 40592476937, "sender_id": 1, "hash_code": "x"}), "40592476937"),
        (json.dumps({"shipment_id": "12345678"}), "12345678"),
        (json.dumps({"shipping_id": 87654321}), "87654321"),
        ("https://www.mercadolibre.com/shipments/40592476937", "40592476937"),
        (None, None),
        ("", None),
        ("   ", None),
        ("no-digits-here", None),
        (json.dumps({"sender_id": 1, "hash_code": "abc"}), None),  # no id-ish key, no long run
    ],
)
def test_parse_flex_qr(raw, expected):
    assert parse_flex_qr(raw) == expected


def test_parse_flex_qr_prefers_id_key_over_other_numbers():
    payload = json.dumps({"id": 999888777, "sender_id": 111})
    assert parse_flex_qr(payload) == "999888777"
