"""Shared contract for geocoder backends.

Every backend takes a single address string and returns `None` (miss) or a
`GeocodeResult` whose `confidence` is already normalized to the 0..1 range.
The orchestration layer (`service.py`) applies a minimum-confidence filter
and the cache.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lng: float
    confidence: float  # 0.0 .. 1.0
    backend: str


class Geocoder(Protocol):
    name: str

    def geocode(self, address: str) -> GeocodeResult | None:
        ...
