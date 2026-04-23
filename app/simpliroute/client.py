"""SimpliRoute REST client.

- `create_visit(payload)` -> POST /v1/routes/visits, returns the created
  visit dict. Retries on 5xx and timeouts with exponential backoff.
- `get_visit(visit_id)` -> GET /v1/routes/visits/{id}, useful for
  post-creation validation.

Errors are split into:
  - TransientSimpliRouteError: 5xx, timeout, network failure. Retried.
  - PermanentSimpliRouteError: 4xx. Not retried; caller routes to
    manual_review.

The `reference` field on every visit is the ML order id, so SimpliRoute
operators can trace any visit back to its source order.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings
from app.core.logging import logger
from app.core.transform import VisitPayload


class SimpliRouteError(Exception):
    def __init__(self, status_code: int | None, body: str, url: str):
        super().__init__(
            f"SimpliRoute {'?' if status_code is None else status_code} on {url}: {body[:200]}"
        )
        self.status_code = status_code
        self.body = body
        self.url = url


class TransientSimpliRouteError(SimpliRouteError):
    """5xx, timeout or connection error — retry."""


class PermanentSimpliRouteError(SimpliRouteError):
    """4xx — do not retry; caller must handle (usually -> manual_review)."""


class SimpliRouteClient:
    def __init__(self, http_client: httpx.Client | None = None):
        self._settings = get_settings()
        if not self._settings.simpliroute_token:
            raise RuntimeError("SIMPLIROUTE_TOKEN is not configured")
        self._http = http_client or httpx.Client(timeout=20.0)
        self._owns_http = http_client is None

    def __enter__(self) -> SimpliRouteClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Token {self._settings.simpliroute_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.simpliroute_api_base}{path}"

    def create_visit(self, payload: VisitPayload) -> dict:
        """Create a visit. Retries transient errors with exponential backoff."""
        body = _render_visit_payload(payload)
        try:
            return self._retrying_post("/v1/routes/visits/", body)
        except RetryError as re:
            # Unwrap the last attempt's real exception for a clean error surface.
            last = re.last_attempt.exception()
            if isinstance(last, SimpliRouteError):
                raise last from re
            raise

    def get_visit(self, visit_id: str | int) -> dict:
        url = self._url(f"/v1/routes/visits/{visit_id}/")
        try:
            response = self._http.get(url, headers=self._headers())
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise TransientSimpliRouteError(None, str(e), url) from e
        _raise_for_status(response, url)
        return response.json()

    def find_visit_by_reference(self, reference: str) -> dict | None:
        """Crash-safe retry guard: look up a visit we may have already created.

        If the process died between POST /visits and the DB completion UPDATE,
        the next run would otherwise create a duplicate. Callers use this in
        the retry path to recover the existing visit instead of re-creating.

        Returns the first matching visit dict or None.
        """
        url = self._url("/v1/routes/visits/")
        try:
            response = self._http.get(
                url, params={"reference": reference}, headers=self._headers()
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise TransientSimpliRouteError(None, str(e), url) from e
        if response.status_code == 404:
            return None
        _raise_for_status(response, url)
        data = response.json()
        # SimpliRoute can return a list or a paginated {results: [...]}.
        results = data.get("results") if isinstance(data, dict) else data
        if not results:
            return None
        for visit in results:
            if str(visit.get("reference")) == str(reference):
                return visit
        return None

    # -- Internal: retry wrapper bound to the instance via a closure so the
    # tenacity decorator's config uses our settings at call time.
    def _retrying_post(self, path: str, body: dict) -> dict:
        max_attempts = self._settings.simpliroute_max_retries
        base = self._settings.simpliroute_backoff_base_seconds

        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=base, min=base, max=16),
            retry=retry_if_exception_type(TransientSimpliRouteError),
            reraise=False,
        )
        def _do() -> dict:
            url = self._url(path)
            try:
                response = self._http.post(
                    url, json=body, headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.warning(
                    "simpliroute_transient_network_error", url=url, error=str(e)
                )
                raise TransientSimpliRouteError(None, str(e), url) from e
            _raise_for_status(response, url)
            return response.json()

        return _do()


def _raise_for_status(response: httpx.Response, url: str) -> None:
    if response.status_code < 400:
        return
    body = response.text
    if response.status_code >= 500:
        logger.warning(
            "simpliroute_transient_http_error", url=url, status=response.status_code
        )
        raise TransientSimpliRouteError(response.status_code, body, url)
    logger.error(
        "simpliroute_permanent_http_error",
        url=url,
        status=response.status_code,
        body=body[:200],
    )
    raise PermanentSimpliRouteError(response.status_code, body, url)


def _render_visit_payload(payload: VisitPayload) -> dict[str, Any]:
    """Convert our internal VisitPayload into SimpliRoute's JSON shape."""
    body: dict[str, Any] = {
        "title": payload.title,
        "address": payload.address,
        "contact_name": payload.contact_name,
        "contact_phone": payload.contact_phone,
        "notes": payload.notes,
        # External ML order id -> traceability.
        "reference": payload.reference,
        # SimpliRoute expects today's planning date by default.
        "planned_date": date.today().isoformat(),
    }
    if payload.latitude is not None and payload.longitude is not None:
        body["latitude"] = payload.latitude
        body["longitude"] = payload.longitude
    if payload.window_start:
        body["window_start"] = payload.window_start
    if payload.window_end:
        body["window_end"] = payload.window_end
    return body
