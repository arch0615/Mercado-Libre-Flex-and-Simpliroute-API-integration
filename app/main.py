from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.logging import configure_logging
from app.db.models import CronRun, CronRunStatus
from app.ml.routes import router as oauth_router
from app.scheduler.routes import router as scheduler_router

configure_logging()

app = FastAPI(title="Mercado Libre Flex -> SimpliRoute")
app.include_router(oauth_router)
app.include_router(scheduler_router)


@app.get("/health")
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
