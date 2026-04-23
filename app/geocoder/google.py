"""Google Maps Geocoding API backend.

Confidence is derived from `geometry.location_type`:
  ROOFTOP              -> 1.00   (exact match, rooftop-level)
  RANGE_INTERPOLATED   -> 0.80   (interpolated between two points)
  GEOMETRIC_CENTER     -> 0.60   (center of a polyline/polygon)
  APPROXIMATE          -> 0.30   (approximate result, e.g. city-level)

A `partial_match: true` response drops confidence by 0.2 since Google
matched only part of the input.
"""
from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.core.logging import logger
from app.geocoder.base import GeocodeResult

API_URL = "https://maps.googleapis.com/maps/api/geocode/json"

_LOCATION_TYPE_CONFIDENCE = {
    "ROOFTOP": 1.00,
    "RANGE_INTERPOLATED": 0.80,
    "GEOMETRIC_CENTER": 0.60,
    "APPROXIMATE": 0.30,
}


class GoogleGeocoder:
    name = "google"

    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=10.0)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def geocode(self, address: str) -> GeocodeResult | None:
        settings = get_settings()
        if not settings.google_maps_api_key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY is not configured")

        params = {
            "address": address,
            "key": settings.google_maps_api_key,
            # Country bias to Argentina.
            "region": "ar",
        }
        response = self._http.get(API_URL, params=params)
        if response.status_code != 200:
            logger.warning(
                "google_geocode_http_error",
                status=response.status_code,
                body=response.text[:200],
            )
            return None

        data = response.json()
        status = data.get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            logger.warning(
                "google_geocode_api_error", status=status, error=data.get("error_message")
            )
            return None

        results = data.get("results") or []
        if not results:
            return None

        top = results[0]
        geometry = top.get("geometry") or {}
        location = geometry.get("location") or {}
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is None or lng is None:
            return None

        confidence = _LOCATION_TYPE_CONFIDENCE.get(geometry.get("location_type"), 0.3)
        if top.get("partial_match"):
            confidence = max(0.0, confidence - 0.2)

        return GeocodeResult(lat=float(lat), lng=float(lng), confidence=confidence, backend=self.name)
