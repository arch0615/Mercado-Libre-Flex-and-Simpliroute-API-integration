from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.logging import logger
from app.ml import oauth
from app.web.templates import templates

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/authorize")
def authorize(state: str | None = Query(default=None)) -> RedirectResponse:
    url = oauth.build_authorize_url(state=state)
    return RedirectResponse(url=url, status_code=302)


def _error_page(request: Request, message: str, http_status: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "oauth_error.html",
        {"now": _now_str(), "error_message": message},
        status_code=http_status,
    )


@router.get("/callback", response_class=HTMLResponse)
def callback(
    request: Request,
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    if error:
        return _error_page(request, f"Mercado Libre returned: {error}", 400)
    if not code:
        return _error_page(request, "Missing `code` query parameter.", 400)

    try:
        payload = oauth.exchange_code_for_token(code)
    except oauth.OAuthError as exc:
        logger.error("ml_oauth_callback_failed", error=str(exc))
        return _error_page(request, str(exc), 502)

    token = oauth.persist_token(session, payload)
    logger.info(
        "ml_oauth_authorized",
        account_id=token.account_id,
        expires_at=token.expires_at.isoformat(),
    )
    return templates.TemplateResponse(
        request,
        "oauth_success.html",
        {
            "now": _now_str(),
            "account_id": token.account_id,
            "expires_at": token.expires_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "refresh_leeway_min": 5,
        },
    )


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
