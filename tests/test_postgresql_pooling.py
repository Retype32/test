"""Regression tests for PG-1 (connection-pool tuning), run against a real
PostgreSQL server.

This module intentionally does NOT go through tests/conftest.py's SQLite
setup -- it builds its own engines directly against the PostgreSQL
databases described in the Phase 3 DB-OPS task (nexus_test role, 5
pre-created nexus_*_test databases), independent of backend.core.config's
settings singleton, so it exercises exactly the pool kwargs
backend.core.database._pool_kwargs() computes without needing the whole app
wired up to Postgres.

Skips (does not fail) if PostgreSQL isn't reachable on localhost:5432, per
the task's instruction that these tests must degrade gracefully in an
environment without a live Postgres server -- but does not skip by default
in this sandbox, where a real server is running.
"""
import asyncio
import socket
import time

import pytest
from sqlalchemy.exc import TimeoutError as SQLATimeoutError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.database import _pool_kwargs

PG_HOST = "localhost"
PG_PORT = 5432
PG_URL = f"postgresql+asyncpg://nexus_test:nexus_test_pw_2026@{PG_HOST}:{PG_PORT}/nexus_core_test"


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason=f"PostgreSQL not reachable on {PG_HOST}:{PG_PORT}",
)


async def test_pool_kwargs_match_pg1_recommended_defaults_for_core_and_catalog():
    """PG-1's recommended defaults, verbatim from the consolidated plan §11:
    core = pool_size 5 / max_overflow 5; each catalog = pool_size 10 /
    max_overflow 10; both timeout 10s, recycle 1800s, pre_ping True."""
    core_kwargs = _pool_kwargs(PG_URL, pool_size=5, max_overflow=5)
    assert core_kwargs == {
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 10,
    }

    catalog_kwargs = _pool_kwargs(PG_URL, pool_size=10, max_overflow=10)
    assert catalog_kwargs == {
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 10,
        "pool_timeout": 10,
    }


async def test_sqlite_url_never_receives_queuepool_only_kwargs():
    """Empirical regression guard for the deviation from the task's initial
    assumption: SQLAlchemy's create_async_engine raises TypeError if
    pool_size/max_overflow/pool_timeout are passed for a SQLite URL (NullPool
    for a file DB, StaticPool for :memory:, neither accepts them) -- so
    _pool_kwargs must never include them for a sqlite:// URL, and passing
    its output to create_async_engine must not raise."""
    kwargs = _pool_kwargs("sqlite+aiosqlite:///./somewhere.db", pool_size=5, max_overflow=5)
    assert kwargs == {"pool_recycle": 1800, "pool_pre_ping": True}
    assert "pool_size" not in kwargs
    assert "max_overflow" not in kwargs
    assert "pool_timeout" not in kwargs

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        engine = create_async_engine(f"sqlite+aiosqlite:///{d}/x.db", **kwargs)
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("select 1")
        finally:
            await engine.dispose()


async def test_engine_reports_configured_pool_size_against_real_postgres():
    engine = create_async_engine(PG_URL, **_pool_kwargs(PG_URL, pool_size=5, max_overflow=5))
    try:
        assert engine.pool.size() == 5
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("select 1")
            assert result.scalar() == 1
    finally:
        await engine.dispose()


async def test_pool_exhaustion_raises_timeout_shaped_error_within_pool_timeout():
    """The concrete PG-1 regression test: with pool_size=5, max_overflow=5
    (capacity 10) and pool_timeout=10s, driving 10 concurrent long-held
    checkouts saturates the pool; the 11th checkout must raise a
    timeout-shaped error (sqlalchemy.exc.TimeoutError) at roughly the
    configured pool_timeout, not hang forever and not succeed."""
    pool_size = 5
    max_overflow = 5
    pool_timeout = 10
    capacity = pool_size + max_overflow  # 10

    engine = create_async_engine(
        PG_URL,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=1800,
        pool_pre_ping=True,
    )
    assert engine.pool.size() == pool_size

    held_count = 0
    held_lock = asyncio.Lock()
    release_event = asyncio.Event()

    async def hold_one():
        nonlocal held_count
        conn = await engine.connect()
        try:
            async with held_lock:
                held_count += 1
            await release_event.wait()
        finally:
            await conn.close()

    holder_tasks = [asyncio.create_task(hold_one()) for _ in range(capacity)]
    try:
        # Wait for all `capacity` connections to actually be checked out
        # (not just tasks scheduled) before attempting the one that should
        # overflow past capacity.
        deadline = time.monotonic() + 15
        while held_count < capacity:
            if time.monotonic() > deadline:
                pytest.fail(
                    f"only {held_count}/{capacity} connections were checked "
                    "out within 15s -- pool did not saturate as expected"
                )
            await asyncio.sleep(0.05)

        start = time.monotonic()
        with pytest.raises(SQLATimeoutError):
            extra_conn = await engine.connect()
            await extra_conn.close()  # pragma: no cover - never reached on pass
        elapsed = time.monotonic() - start

        # Should fail at roughly pool_timeout (10s), not immediately and not
        # after hanging indefinitely. Generous bounds for a real network
        # round-trip to a real server plus scheduler jitter.
        assert elapsed < pool_timeout + 10, (
            f"pool checkout took {elapsed:.1f}s to raise, expected close to "
            f"{pool_timeout}s -- looks like it hung instead of timing out"
        )
    finally:
        release_event.set()
        await asyncio.gather(*holder_tasks, return_exceptions=True)
        await engine.dispose()
