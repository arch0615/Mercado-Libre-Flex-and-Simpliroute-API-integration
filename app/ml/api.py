"""Thin authenticated wrapper around the Mercado Libre REST API.

Uses `get_valid_access_token` so every call transparently refreshes the
access token when close to expiry. The underlying httpx client can be
injected for testing.
"""
from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.ml.oauth import get_valid_access_token


class MLAPIError(Exception):
    """Raised when a ML API call returns a non-2xx status."""

    def __init__(self, status_code: int, body: str, url: str):
        super().__init__(f"ML API {status_code} on {url}: {body[:200]}")
        self.status_code = status_code
        self.body = body
        self.url = url


class MLClient:
    """Stateful authenticated client for a single cron run.

    Holds one httpx.Client for connection reuse. Callers are expected to use
    it as a context manager or call `close()`.
    """

    def __init__(self, session: Session, http_client: httpx.Client | None = None):
        self._session = session
        self._settings = get_settings()
        self._http = http_client or httpx.Client(timeout=20.0)
        self._owns_http = http_client is None

    def __enter__(self) -> MLClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _headers(self) -> dict:
        token = get_valid_access_token(self._session, http_client=self._http)
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self._url(path)
        resp = self._http.get(url, params=params, headers=self._headers())
        if resp.status_code >= 400:
            raise MLAPIError(resp.status_code, resp.text, url)
        return resp.json()

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self._settings.ml_api_base}{path}"
