from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import GeocodingCache
from app.geocoder.base import GeocodeResult
from app.geocoder.google import API_URL as GOOGLE_URL
from app.geocoder.google import GoogleGeocoder
from app.geocoder.nominatim import API_URL as NOMINATIM_URL
from app.geocoder.nominatim import NominatimGeocoder
from app.geocoder.service import _hash, resolve


@pytest.fixture(autouse=True)
def _settings_with_google_key(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-google-key")
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_nominatim_rate_limit():
    # Start each test from ts=0 so no real sleep is triggered.
    import app.geocoder.nominatim as mod

    mod._last_call_ts = 0.0
    yield


# ---- Google ----


@respx.mock
def test_google_returns_result_with_rooftop_confidence():
    respx.get(GOOGLE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "geometry": {
                            "location": {"lat": -34.6037, "lng": -58.3816},
                            "location_type": "ROOFTOP",
                        }
                    }
                ],
            },
        )
    )
    geo = GoogleGeocoder()
    result = geo.geocode("Av. Corrientes 1234, CABA, Argentina")
    assert result is not None
    assert result.confidence == 1.0
    assert result.backend == "google"
    assert result.lat == -34.6037


@respx.mock
def test_google_partial_match_reduces_confidence():
    respx.get(GOOGLE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "partial_match": True,
                        "geometry": {
                            "location": {"lat": -34.6, "lng": -58.4},
                            "location_type": "ROOFTOP",
                        },
                    }
                ],
            },
        )
    )
    result = GoogleGeocoder().geocode("Calle Imaginaria 999")
    assert result is not None
    # 1.0 - 0.2 penalty
    assert abs(result.confidence - 0.8) < 1e-6


@respx.mock
def test_google_zero_results_returns_none():
    respx.get(GOOGLE_URL).mock(
        return_value=httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
    )
    assert GoogleGeocoder().geocode("asdfgh 9999, Marte") is None


@respx.mock
def test_google_http_error_returns_none():
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(500, text="server fire"))
    assert GoogleGeocoder().geocode("anywhere") is None


@respx.mock
def test_google_approximate_location_gets_low_confidence():
    respx.get(GOOGLE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "geometry": {
                            "location": {"lat": -34.6, "lng": -58.4},
                            "location_type": "APPROXIMATE",
                        }
                    }
                ],
            },
        )
    )
    result = GoogleGeocoder().geocode("Argentina")
    assert result is not None
    assert result.confidence == 0.3


def test_google_raises_if_key_missing(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="GOOGLE_MAPS_API_KEY"):
        GoogleGeocoder().geocode("something")


# ---- Nominatim ----


@respx.mock
def test_nominatim_returns_result():
    respx.get(NOMINATIM_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "lat": "-34.60",
                    "lon": "-58.38",
                    "importance": 0.85,
                }
            ],
        )
    )
    result = NominatimGeocoder().geocode("Av Corrientes 1234, CABA")
    assert result is not None
    assert result.backend == "nominatim"
    assert result.lat == -34.60
    assert result.confidence == 0.85


@respx.mock
def test_nominatim_empty_list_returns_none():
    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    assert NominatimGeocoder().geocode("nonexistent") is None


# ---- Cache / service ----


@respx.mock
def test_resolve_caches_on_success(session):
    respx.get(GOOGLE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "geometry": {
                            "location": {"lat": -34.5, "lng": -58.4},
                            "location_type": "ROOFTOP",
                        }
                    }
                ],
            },
        )
    )
    geo = GoogleGeocoder()
    result = resolve(session, "Av. Corrientes 1234", backend=geo)
    assert result is not None
    session.commit()

    cached = session.execute(
        select(GeocodingCache).where(
            GeocodingCache.address_hash == _hash("Av. Corrientes 1234")
        )
    ).scalar_one()
    assert cached.lat == -34.5
    assert cached.backend == "google"


@respx.mock
def test_resolve_hits_cache_without_calling_backend(session):
    address = "Calle Cacheada 500"
    session.add(
        GeocodingCache(
            address_hash=_hash(address),
            normalized_address=address,
            lat=-34.1,
            lng=-58.2,
            confidence=0.95,
            backend="google",
        )
    )
    session.commit()

    # No respx mock: any HTTP call would fail. Cache must satisfy the call.
    result = resolve(session, address, backend=GoogleGeocoder())
    assert result is not None
    assert result.lat == -34.1
    assert result.lng == -58.2
    assert result.confidence == 0.95


@respx.mock
def test_resolve_does_not_cache_misses(session):
    respx.get(GOOGLE_URL).mock(
        return_value=httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
    )
    result = resolve(session, "calle fantasma 9999", backend=GoogleGeocoder())
    assert result is None

    # Cache should be empty — we want to retry next cron run.
    assert session.execute(select(GeocodingCache)).scalar_one_or_none() is None


def test_resolve_empty_address_returns_none(session):
    assert resolve(session, "", backend=GoogleGeocoder()) is None


def test_geocode_result_is_frozen():
    r = GeocodeResult(lat=1.0, lng=2.0, confidence=0.9, backend="google")
    with pytest.raises(Exception):
        r.confidence = 0.0  # frozen dataclass
