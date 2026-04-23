"""PostgreSQL session-level advisory lock.

Used at the top of every scheduler run to guarantee that only one worker
is processing orders at a time. If the lock can't be acquired, the run
exits cleanly (logged + returned as `skipped`) instead of blocking.

On non-PostgreSQL dialects (SQLite in tests) the context manager is a
no-op so the rest of the code runs unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

# Arbitrary bigint used as the advisory lock key. Any fixed integer works;
# keeping a named constant makes ops debugging easier (hex 'MLFX' tag).
DEFAULT_LOCK_KEY = 0x4D4C4658  # 'MLFX'


class LockNotAcquired(Exception):
    """Raised when pg_try_advisory_lock returns false (another worker holds it)."""


@contextmanager
def advisory_lock(session: Session, key: int = DEFAULT_LOCK_KEY) -> Iterator[None]:
    """Acquire a Postgres session-level advisory lock for the block's duration.

    - Succeeds immediately and yields if the lock is free.
    - Raises `LockNotAcquired` if another session already holds it.
    - No-op on non-Postgres dialects (for SQLite-backed unit tests).
    """
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect != "postgresql":
        logger.debug("advisory_lock_noop", dialect=dialect)
        yield
        return

    acquired = session.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
    ).scalar()
    if not acquired:
        raise LockNotAcquired(f"Advisory lock {key:#x} already held")

    logger.debug("advisory_lock_acquired", key=f"{key:#x}")
    try:
        yield
    finally:
        session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        logger.debug("advisory_lock_released", key=f"{key:#x}")
