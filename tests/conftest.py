"""Shared test fixtures.

Unit tests use in-memory SQLite. We override the JSONB type compilation so
the production models (which use JSONB) can still have their tables created
against SQLite without changes.
"""
from __future__ import annotations

import os

# Set env before importing app modules that read settings at import time.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ML_CLIENT_ID", "test-client-id")
os.environ.setdefault("ML_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("ML_REDIRECT_URI", "http://testserver/oauth/callback")
os.environ.setdefault("ML_API_BASE", "https://api.mercadolibre.com")
os.environ.setdefault("ML_AUTH_BASE", "https://auth.mercadolibre.com.ar")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


from app.db import models  # noqa: E402, F401
from app.db.base import Base  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = maker()
    try:
        yield s
    finally:
        s.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()
        s.close()
