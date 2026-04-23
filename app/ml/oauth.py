"""Mercado Libre OAuth 2.0 client.

Key guarantees:
- Refresh tokens are rotated: every successful refresh returns a new
  refresh_token; we persist it atomically so we never lose the chain.
- `get_valid_access_token()` returns a token guaranteed to be valid for
  at least `REFRESH_LEEWAY_SECONDS` more seconds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.db.models import OAuthProvider, OAuthToken

REFRESH_LEEWAY_SECONDS = 300  # refresh 5 min before expiry


class OAuthError(Exception):
    """Raised when Mercado Libre OAuth exchange fails."""


@dataclass
class TokenPayload:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str | None
    scope: str | None

    @classmethod
    def from_response(cls, data: dict) -> TokenPayload:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=int(data.get("expires_in", 21600)),
            user_id=str(data["user_id"]) if data.get("user_id") is not None else None,
            scope=data.get("scope"),
        )


def build_authorize_url(state: str | None = None) -> str:
    """URL the seller must open to authorize the application."""
    settings = get_settings()
    params = {
        "response_type": "code",
        "client_id": settings.ml_client_id,
        "redirect_uri": settings.ml_redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{settings.ml_auth_base}/authorization?{urlencode(params)}"


def _token_endpoint() -> str:
    return f"{get_settings().ml_api_base}/oauth/token"


def exchange_code_for_token(code: str, *, http_client: httpx.Client | None = None) -> TokenPayload:
    """Exchange an authorization code for an access + refresh token pair."""
    settings = get_settings()
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.ml_client_id,
        "client_secret": settings.ml_client_secret,
        "code": code,
        "redirect_uri": settings.ml_redirect_uri,
    }
    return _post_token(payload, http_client=http_client)


def refresh_access_token(
    refresh_token: str, *, http_client: httpx.Client | None = None
) -> TokenPayload:
    """Refresh an access token. ML rotates the refresh_token on every call."""
    settings = get_settings()
    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.ml_client_id,
        "client_secret": settings.ml_client_secret,
        "refresh_token": refresh_token,
    }
    return _post_token(payload, http_client=http_client)


def _post_token(payload: dict, *, http_client: httpx.Client | None) -> TokenPayload:
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=15.0)
    try:
        response = client.post(
            _token_endpoint(),
            data=payload,
            headers={"Accept": "application/json"},
        )
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        logger.error(
            "ml_oauth_error",
            status=response.status_code,
            body=response.text[:500],
            grant_type=payload.get("grant_type"),
        )
        raise OAuthError(
            f"ML OAuth failed: HTTP {response.status_code} grant={payload.get('grant_type')}"
        )
    return TokenPayload.from_response(response.json())


def persist_token(session: Session, payload: TokenPayload) -> OAuthToken:
    """Upsert the stored token for Mercado Libre. Rotation-safe."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload.expires_in)
    token = session.execute(
        select(OAuthToken).where(OAuthToken.provider == OAuthProvider.mercadolibre)
    ).scalar_one_or_none()
    if token is None:
        token = OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            expires_at=expires_at,
            account_id=payload.user_id,
            scope=payload.scope,
        )
        session.add(token)
    else:
        token.access_token = payload.access_token
        token.refresh_token = payload.refresh_token
        token.expires_at = expires_at
        if payload.user_id:
            token.account_id = payload.user_id
        if payload.scope:
            token.scope = payload.scope
    session.flush()
    return token


def get_valid_access_token(
    session: Session, *, http_client: httpx.Client | None = None
) -> str:
    """Return an access token valid for at least REFRESH_LEEWAY_SECONDS more."""
    token = session.execute(
        select(OAuthToken).where(OAuthToken.provider == OAuthProvider.mercadolibre)
    ).scalar_one_or_none()
    if token is None:
        raise OAuthError(
            "No ML OAuth token on file. Run /oauth/authorize to initialize."
        )

    now = datetime.now(timezone.utc)
    expires_at = token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at - now > timedelta(seconds=REFRESH_LEEWAY_SECONDS):
        return token.access_token

    logger.info(
        "ml_oauth_refresh",
        expires_at=token.expires_at.isoformat(),
        leeway_seconds=REFRESH_LEEWAY_SECONDS,
    )
    payload = refresh_access_token(token.refresh_token, http_client=http_client)
    refreshed = persist_token(session, payload)
    return refreshed.access_token
