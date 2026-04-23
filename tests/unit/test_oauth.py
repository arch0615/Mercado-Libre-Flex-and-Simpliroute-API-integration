from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import OAuthProvider, OAuthToken
from app.ml import oauth


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _token_url() -> str:
    return f"{get_settings().ml_api_base}/oauth/token"


def test_build_authorize_url_contains_required_params():
    url = oauth.build_authorize_url(state="abc123")
    assert url.startswith(get_settings().ml_auth_base + "/authorization?")
    assert "response_type=code" in url
    assert "client_id=test-client-id" in url
    assert "state=abc123" in url
    assert "redirect_uri=" in url


@respx.mock
def test_exchange_code_for_token_returns_payload():
    respx.post(_token_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT-1",
                "refresh_token": "RT-1",
                "expires_in": 21600,
                "user_id": 123456,
                "scope": "offline_access read write",
            },
        )
    )
    payload = oauth.exchange_code_for_token("dummy-code")
    assert payload.access_token == "AT-1"
    assert payload.refresh_token == "RT-1"
    assert payload.expires_in == 21600
    assert payload.user_id == "123456"


@respx.mock
def test_exchange_code_raises_on_4xx():
    respx.post(_token_url()).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code_for_token("bad-code")


@respx.mock
def test_refresh_rotates_refresh_token(session):
    # Seed an expired token.
    session.add(
        OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token="OLD-AT",
            refresh_token="OLD-RT",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    session.commit()

    respx.post(_token_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "NEW-AT",
                "refresh_token": "NEW-RT",
                "expires_in": 21600,
                "user_id": 123456,
            },
        )
    )

    at = oauth.get_valid_access_token(session)
    assert at == "NEW-AT"

    stored = session.execute(
        select(OAuthToken).where(OAuthToken.provider == OAuthProvider.mercadolibre)
    ).scalar_one()
    # Rotation must have persisted the new refresh token.
    assert stored.refresh_token == "NEW-RT"
    assert stored.access_token == "NEW-AT"
    assert stored.account_id == "123456"
    # New expiry should be in the future (SQLite strips tzinfo, normalize).
    exp = stored.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    assert exp > datetime.now(timezone.utc)


def test_get_valid_access_token_skips_refresh_when_fresh(session):
    future = datetime.now(timezone.utc) + timedelta(hours=5)
    session.add(
        OAuthToken(
            provider=OAuthProvider.mercadolibre,
            access_token="FRESH-AT",
            refresh_token="FRESH-RT",
            expires_at=future,
        )
    )
    session.commit()

    # Any network call would raise because respx isn't mocking anything here.
    at = oauth.get_valid_access_token(session)
    assert at == "FRESH-AT"


def test_get_valid_access_token_raises_when_not_initialized(session):
    with pytest.raises(oauth.OAuthError):
        oauth.get_valid_access_token(session)


@respx.mock
def test_persist_token_upserts(session):
    p1 = oauth.TokenPayload(
        access_token="AT-1", refresh_token="RT-1", expires_in=100, user_id="1", scope=None
    )
    oauth.persist_token(session, p1)
    session.commit()

    p2 = oauth.TokenPayload(
        access_token="AT-2", refresh_token="RT-2", expires_in=200, user_id="1", scope="s"
    )
    oauth.persist_token(session, p2)
    session.commit()

    rows = session.execute(select(OAuthToken)).scalars().all()
    assert len(rows) == 1
    assert rows[0].access_token == "AT-2"
    assert rows[0].refresh_token == "RT-2"
