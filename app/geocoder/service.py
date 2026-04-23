"""Cached geocoder facade.

Callers use `resolve(session, address)` which:
  1. Hashes the address, checks `geocoding_cache`.
  2. On cache miss, calls the configured backend.
  3. Persists successful results to the cache (negative results are NOT
     cached; the order will be retried next run).

Confidence threshold filtering stays in the caller (see `scheduler/processor.py`)
so this module stays purely about coordinates vs. no-coordinates.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.db.models import GeocodingCache
from app.geocoder.base import Geocoder, GeocodeResult
from app.geocoder.google import GoogleGeocoder
from app.geocoder.nominatim import NominatimGeocoder


def _hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()


def build_backend() -> Geocoder:
    backend = get_settings().geocoder_backend
    if backend == "google":
        return GoogleGeocoder()
    if backend == "nominatim":
        return NominatimGeocoder()
    raise ValueError(f"Unknown geocoder backend: {backend}")


def resolve(
    session: Session,
    address: str,
    *,
    backend: Geocoder | None = None,
) -> GeocodeResult | None:
    """Resolve a normalized address to coordinates, using the cache first."""
    if not address:
        return None

    key = _hash(address)
    cached = session.execute(
        select(GeocodingCache).where(GeocodingCache.address_hash == key)
    ).scalar_one_or_none()
    if cached and cached.lat is not None and cached.lng is not None:
        logger.debug("geocode_cache_hit", address=address, backend=cached.backend)
        return GeocodeResult(
            lat=cached.lat,
            lng=cached.lng,
            confidence=cached.confidence or 0.0,
            backend=cached.backend,
        )

    bk = backend or build_backend()
    try:
        result = bk.geocode(address)
    finally:
        if backend is None and hasattr(bk, "close"):
            bk.close()

    if result is None:
        logger.info("geocode_miss", address=address, backend=bk.name)
        return None

    _write_cache(session, key, address, result)
    logger.info(
        "geocode_ok",
        address=address,
        backend=result.backend,
        confidence=result.confidence,
    )
    return result


def _write_cache(
    session: Session, key: str, address: str, result: GeocodeResult
) -> None:
    existing = session.execute(
        select(GeocodingCache).where(GeocodingCache.address_hash == key)
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            GeocodingCache(
                address_hash=key,
                normalized_address=address,
                lat=result.lat,
                lng=result.lng,
                confidence=result.confidence,
                backend=result.backend,
            )
        )
    else:
        existing.lat = result.lat
        existing.lng = result.lng
        existing.confidence = result.confidence
        existing.backend = result.backend
    session.flush()
