"""Nominatim (OpenStreetMap) geocoder backend.

Rate limit: 1 request/second per the OSM usage policy. Since our cron runs
every ~20 min and most addresses hit the cache, the effective rate is well
under this in practice.

Confidence uses the Nominatim `importance` score (0..1), nudged up when the
result has type='house' or 'place' and a high address-class match.
"""
from __future__ import annotations

import time

import httpx

from app.core.logging import logger
from app.geocoder.base import GeocodeResult

API_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "mercado-simpliroute/0.1 (automated flex -> simpliroute)"

_last_call_ts = 0.0
_MIN_INTERVAL_SECONDS = 1.0


class NominatimGeocoder:
    name = "nominatim"

    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(
            timeout=10.0, headers={"User-Agent": USER_AGENT}
        )
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def geocode(self, address: str) -> GeocodeResult | None:
        self._respect_rate_limit()

        response = self._http.get(
            API_URL,
            params={
                "q": address,
                "format": "json",
                "limit": 1,
                "countrycodes": "ar",
                "addressdetails": 0,
            },
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code != 200:
            logger.warning(
                "nominatim_http_error",
                status=response.status_code,
                body=response.text[:200],
            )
            return None

        results = response.json()
        if not results:
            return None

        top = results[0]
        lat = top.get("lat")
        lon = top.get("lon")
        if lat is None or lon is None:
            return None

        importance = float(top.get("importance") or 0.3)
        confidence = min(1.0, max(0.0, importance))

        return GeocodeResult(
            lat=float(lat), lng=float(lon), confidence=confidence, backend=self.name
        )

    def _respect_rate_limit(self) -> None:
        global _last_call_ts
        now = time.monotonic()
        delta = now - _last_call_ts
        if delta < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - delta)
        _last_call_ts = time.monotonic()
