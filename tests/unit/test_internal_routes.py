from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_db
from app.db.models import CronRunStatus
from app.scheduler.job import RunSummary


@pytest.fixture
def client(session, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("INTERNAL_SECRET", "correct-horse-battery-staple")

    from app.main import app

    def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_internal_run_rejects_missing_secret(client):
    response = client.post("/internal/run")
    assert response.status_code == 401
    assert "Missing" in response.json()["detail"]


def test_internal_run_rejects_wrong_secret(client):
    response = client.post(
        "/internal/run", headers={"X-Internal-Secret": "nope"}
    )
    assert response.status_code == 401


def test_internal_run_accepts_correct_secret_and_triggers_run_sync(client):
    summary = RunSummary(
        cron_run_id=42,
        status=CronRunStatus.success,
        processed=3,
        skipped=0,
        errors=0,
        manual_review=1,
    )
    with patch("app.scheduler.routes.run_sync", return_value=summary) as m:
        response = client.post(
            "/internal/run",
            headers={"X-Internal-Secret": "correct-horse-battery-staple"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "cron_run_id": 42,
        "status": "success",
        "processed": 3,
        "skipped": 0,
        "manual_review": 1,
        "errors": 0,
    }
    m.assert_called_once()
