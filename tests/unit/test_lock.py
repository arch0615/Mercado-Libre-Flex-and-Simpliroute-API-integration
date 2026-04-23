from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.scheduler.lock import DEFAULT_LOCK_KEY, LockNotAcquired, advisory_lock


def test_advisory_lock_is_noop_on_sqlite(session):
    # SQLite bound session -> dialect 'sqlite' -> noop context manager.
    entered = False
    with advisory_lock(session):
        entered = True
    assert entered


def _make_postgres_session(*, acquired: bool) -> MagicMock:
    """Build a minimal mock Session that looks like a PG-bound session."""
    dialect = MagicMock()
    dialect.name = "postgresql"
    bind = MagicMock()
    bind.dialect = dialect

    session = MagicMock()
    session.bind = bind
    scalar = MagicMock(return_value=acquired)
    session.execute.return_value.scalar = scalar
    return session


def test_advisory_lock_raises_when_held():
    session = _make_postgres_session(acquired=False)
    with pytest.raises(LockNotAcquired):
        with advisory_lock(session):
            pass  # pragma: no cover
    # Should NOT call unlock because it never entered the lock body.
    sqls = [str(call.args[0]) for call in session.execute.call_args_list]
    assert any("pg_try_advisory_lock" in s for s in sqls)
    assert not any("pg_advisory_unlock" in s for s in sqls)


def test_advisory_lock_releases_on_exit():
    session = _make_postgres_session(acquired=True)
    with advisory_lock(session):
        pass
    sqls = [str(call.args[0]) for call in session.execute.call_args_list]
    assert any("pg_try_advisory_lock" in s for s in sqls)
    assert any("pg_advisory_unlock" in s for s in sqls)


def test_advisory_lock_releases_even_on_exception():
    session = _make_postgres_session(acquired=True)
    with pytest.raises(RuntimeError):
        with advisory_lock(session):
            raise RuntimeError("boom")
    sqls = [str(call.args[0]) for call in session.execute.call_args_list]
    assert any("pg_advisory_unlock" in s for s in sqls)


def test_default_lock_key_is_fixed():
    # Documenting intent: key must be stable across deploys; a drift would
    # cause two app versions to not see each other's lock.
    assert DEFAULT_LOCK_KEY == 0x4D4C4658
