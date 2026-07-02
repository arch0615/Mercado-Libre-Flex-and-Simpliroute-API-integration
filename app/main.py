from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.logging import configure_logging
from app.db.models import CronRun, CronRunStatus
from app.ml.routes import router as oauth_router
from app.ml.webhooks import router as ml_webhook_router
from app.scheduler.routes import router as scheduler_router
from app.web.views import router as web_router

configure_logging()

app = FastAPI(
    title="Mercado Libre Flex → SimpliRoute",
    description=(
        "Headless integration that polls Mercado Libre for new Flex orders "
        "and registers them as visits in SimpliRoute. Idempotent, tolerant "
        "to restarts, alerted on failure via Telegram.\n\n"
        "**Operator surfaces:** see [`/`](/), [`/status`](/status). "
        "**Programmatic surfaces:** the routes below."
    ),
    version="1.0.0",
    # Custom themed reference at /docs (see app/web/views.py).
    # Native Swagger UI moved to /swagger for interactive try-it-out.
    docs_url="/swagger",
    redoc_url=None,
    openapi_tags=[
        {"name": "oauth", "description": "Mercado Libre OAuth 2.0 flow."},
        {"name": "ml", "description": "Mercado Libre webhook receiver (low-latency order sync)."},
        {"name": "internal", "description": "Cron-triggered endpoints. Require `X-Internal-Secret` header."},
    ],
)
app.include_router(web_router)
app.include_router(oauth_router)
app.include_router(ml_webhook_router)
app.include_router(scheduler_router)


@app.get("/health", tags=["health"], summary="Liveness + DB + last-cron probe")
def health(session: Session = Depends(get_db)) -> dict:
    try:
        last_run = session.execute(
            select(CronRun)
            .where(CronRun.status == CronRunStatus.success)
            .order_by(CronRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        db_ok = True
    except Exception:
        last_run = None
        db_ok = False

    payload: dict = {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"}
    if last_run and last_run.finished_at:
        age = (datetime.now(timezone.utc) - last_run.finished_at).total_seconds()
        payload["last_cron_run"] = last_run.finished_at.isoformat()
        payload["last_cron_age_seconds"] = int(age)
    else:
        payload["last_cron_run"] = None
    return payload
