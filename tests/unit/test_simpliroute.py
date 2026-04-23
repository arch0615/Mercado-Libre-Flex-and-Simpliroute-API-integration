from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.core.transform import VisitPayload
from app.simpliroute.client import (
    PermanentSimpliRouteError,
    SimpliRouteClient,
    TransientSimpliRouteError,
    _render_visit_payload,
)


@pytest.fixture(autouse=True)
def _settings_with_simpliroute_token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SIMPLIROUTE_TOKEN", "test-token")
    # Smaller backoff so tenacity finishes quickly in tests.
    monkeypatch.setenv("SIMPLIROUTE_MAX_RETRIES", "3")
    monkeypatch.setenv("SIMPLIROUTE_BACKOFF_BASE_SECONDS", "0.01")
    yield
    get_settings.cache_clear()


def _visit() -> VisitPayload:
    return VisitPayload(
        reference="ML-123",
        title="Juan Pérez — ML#ML-123",
        address="Avenida Corrientes 1234, CABA, Argentina",
        latitude=-34.6037,
        longitude=-58.3816,
        contact_name="Juan Pérez",
        contact_phone="011-44445555",
        notes="1x Auriculares",
    )


def _visits_url() -> str:
    return f"{get_settings().simpliroute_api_base}/v1/routes/visits/"


def _visit_url(visit_id: str) -> str:
    return f"{get_settings().simpliroute_api_base}/v1/routes/visits/{visit_id}/"


def test_render_payload_has_reference_and_coordinates():
    body = _render_visit_payload(_visit())
    assert body["reference"] == "ML-123"
    assert body["latitude"] == -34.6037
    assert body["longitude"] == -58.3816
    assert body["contact_phone"] == "011-44445555"
    assert "planned_date" in body


def test_render_payload_omits_missing_coordinates():
    visit = _visit()
    visit.latitude = None
    visit.longitude = None
    body = _render_visit_payload(visit)
    assert "latitude" not in body
    assert "longitude" not in body


def test_client_raises_without_token(monkeypatch):
    monkeypatch.setenv("SIMPLIROUTE_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SIMPLIROUTE_TOKEN"):
        SimpliRouteClient()


@respx.mock
def test_create_visit_happy_path():
    respx.post(_visits_url()).mock(
        return_value=httpx.Response(201, json={"id": 777, "reference": "ML-123"})
    )
    with SimpliRouteClient() as client:
        result = client.create_visit(_visit())
    assert result["id"] == 777


@respx.mock
def test_create_visit_sends_authorization_header():
    route = respx.post(_visits_url()).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    with SimpliRouteClient() as client:
        client.create_visit(_visit())
    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Token test-token"


@respx.mock
def test_create_visit_retries_5xx_then_succeeds():
    route = respx.post(_visits_url())
    route.side_effect = [
        httpx.Response(500, text="upstream fire"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(201, json={"id": 888}),
    ]
    with SimpliRouteClient() as client:
        result = client.create_visit(_visit())
    assert result["id"] == 888
    assert route.call_count == 3


@respx.mock
def test_create_visit_raises_transient_after_max_retries():
    route = respx.post(_visits_url()).mock(
        return_value=httpx.Response(503, text="overloaded")
    )
    with SimpliRouteClient() as client, pytest.raises(TransientSimpliRouteError):
        client.create_visit(_visit())
    # max_retries=3 per fixture
    assert route.call_count == 3


@respx.mock
def test_create_visit_4xx_is_permanent_no_retry():
    route = respx.post(_visits_url()).mock(
        return_value=httpx.Response(400, text="bad address")
    )
    with SimpliRouteClient() as client, pytest.raises(PermanentSimpliRouteError) as exc_info:
        client.create_visit(_visit())
    assert route.call_count == 1
    assert exc_info.value.status_code == 400


@respx.mock
def test_create_visit_401_is_permanent_no_retry():
    route = respx.post(_visits_url()).mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    with SimpliRouteClient() as client, pytest.raises(PermanentSimpliRouteError):
        client.create_visit(_visit())
    assert route.call_count == 1


@respx.mock
def test_create_visit_timeout_is_transient():
    route = respx.post(_visits_url()).mock(side_effect=httpx.TimeoutException("slow"))
    with SimpliRouteClient() as client, pytest.raises(TransientSimpliRouteError):
        client.create_visit(_visit())
    # Transient -> retried up to max_retries
    assert route.call_count == 3


@respx.mock
def test_get_visit():
    respx.get(_visit_url("777")).mock(
        return_value=httpx.Response(200, json={"id": 777, "status": "pending"})
    )
    with SimpliRouteClient() as client:
        result = client.get_visit("777")
    assert result["id"] == 777
