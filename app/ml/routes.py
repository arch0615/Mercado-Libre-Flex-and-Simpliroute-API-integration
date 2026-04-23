from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.logging import logger
from app.ml import oauth

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/authorize")
def authorize(state: str | None = Query(default=None)) -> RedirectResponse:
    url = oauth.build_authorize_url(state=state)
    return RedirectResponse(url=url, status_code=302)


@router.get("/callback")
def callback(
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> dict:
    if error:
        raise HTTPException(status_code=400, detail=f"Authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing `code` query parameter")

    try:
        payload = oauth.exchange_code_for_token(code)
    except oauth.OAuthError as exc:
        logger.error("ml_oauth_callback_failed", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    token = oauth.persist_token(session, payload)
    logger.info(
        "ml_oauth_authorized",
        account_id=token.account_id,
        expires_at=token.expires_at.isoformat(),
    )
    return {
        "status": "ok",
        "account_id": token.account_id,
        "expires_at": token.expires_at.isoformat(),
    }
