"""Real `alembic upgrade head` execution against PostgreSQL (Agent 4's
Phase 5 checks 1-3, docs/production_readiness/04_postgresql_and_reconciliation.md
Section 3).

Every test here is a plain `def`, not `async def`: `alembic.command.upgrade/
downgrade` call `asyncio.run(...)` internally (see alembic/env.py and
alembic_catalog/env.py), which raises "cannot be called from a running
event loop" if invoked from inside pytest-asyncio's own loop. Scratch
database setup/teardown uses `asyncio.run()` directly for the same reason.

Skips (not fails) if PostgreSQL isn't reachable at PG_HOST:PG_PORT, so this
suite stays green in environments without a live server -- it did run for
real, with real assertions, during this session's Phase 5 validation.

This module exists because init_databases()'s own fresh-install path uses
`metadata.create_all()` + `command.stamp(cfg, "head")` (bookkeeping only,
never actually replays a single migration script), which is exercised
elsewhere (test_backup_restore.py's round-trip tests already run it
against real PostgreSQL). Nobody had ever run the migration *scripts*
themselves against PostgreSQL before this session -- doing so here found
and this repo's history now carries the fix for a real bug: the FK-
ondelete migration (d30bfe1ca59a) assumed a constraint name that only
holds under SQLite's batch-mode naming-convention trick; PostgreSQL
assigns its own default constraint names, so the migration's
`drop_constraint('fk_denominations_transaction_id_transactions', ...)`
raised "No such constraint" the first time this was ever tried for real.
"""
import argparse
import asyncio
import uuid
from contextlib import contextmanager

import pytest
from alembic.config import Config
from alembic import command

PG_HOST = "localhost"
PG_PORT = 5432
PG_USER = "nexus_test"
PG_PASSWORD = "nexus_test_pw_2026"


def _postgres_reachable() -> bool:
    import socket
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=2):
            return True
    except OSError:
        return False


def _pg_url(dbname: str) -> str:
    return f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"


@contextmanager
def _pointed_at_core(url: str):
    """alembic/env.py always re-derives its URL from
    backend.core.config.settings.database_url_core at import time (fresh
    each command invocation) -- Config.set_main_option() alone gets
    overwritten by that, so the settings singleton itself must be patched
    for the duration of the alembic command."""
    from backend.core.config import settings
    original = settings.database_url_core
    settings.database_url_core = url
    try:
        yield
    finally:
        settings.database_url_core = original


@contextmanager
def _pointed_at_catalog(code_value: str, url: str):
    """Same story as _pointed_at_core, but alembic_catalog/env.py resolves
    via catalog_db_url(), which reads from the _CATALOG_URLS dict built
    once at backend.core.catalogs import time -- that dict, not
    settings.database_url_vms itself, is what needs patching."""
    from backend.core.catalogs import CatalogCode, _CATALOG_URLS
    code = CatalogCode(code_value)
    original = _CATALOG_URLS[code]
    _CATALOG_URLS[code] = url
    try:
        yield
    finally:
        _CATALOG_URLS[code] = original


async def _admin_conn():
    import asyncpg
    return await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database="postgres")


def _create_scratch_db() -> str:
    name = f"nexus_migtest_{uuid.uuid4().hex[:10]}"

    async def go():
        conn = await _admin_conn()
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(go())
    return name


def _drop_scratch_db(name: str) -> None:
    async def go():
        conn = await _admin_conn()
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()

    asyncio.run(go())


def _table_names(dbname: str) -> set[str]:
    async def go():
        import asyncpg
        conn = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=dbname)
        try:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            return {r["tablename"] for r in rows}
        finally:
            await conn.close()

    return asyncio.run(go())


def _insert_invalid_enum_raises(dbname: str) -> bool:
    """Attempts to insert a Transaction row with a balance_status value
    outside the real enum -- returns True if PostgreSQL rejected it."""
    async def go():
        import asyncpg
        conn = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=dbname)
        try:
            await conn.execute(
                "INSERT INTO customers (customer_id, customer_name) VALUES ('MIG-C', 'x') ON CONFLICT DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO locations (location_id, customer_id, location_name) "
                "VALUES ('MIG-L', 'MIG-C', 'x') ON CONFLICT DO NOTHING"
            )
            try:
                await conn.execute(
                    "INSERT INTO transactions (transaction_id, user_id, username, customer_id, "
                    "location_id, bag_number, total_value, balance_status, business_date) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'x', 'MIG-C', 'MIG-L', "
                    "'310000000098', 1.00, 'NOT_A_REAL_STATUS', CURRENT_DATE)"
                )
                return False  # should never reach here
            except asyncpg.exceptions.InvalidTextRepresentationError:
                return True
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.mark.skipif(not _postgres_reachable(), reason="PostgreSQL not reachable at localhost:5432")
def test_catalog_migrations_run_from_empty_against_postgres():
    dbname = _create_scratch_db()
    try:
        cfg = Config("alembic_catalog.ini")
        cfg.cmd_opts = argparse.Namespace(x=["catalog=vms"])

        with _pointed_at_catalog("vms", _pg_url(dbname)):
            command.upgrade(cfg, "head")  # must not raise -- this is the regression test

        tables = _table_names(dbname)
        for expected in ("transactions", "denominations", "customers", "locations",
                          "eod_closures", "notifications", "duplicate_flags",
                          "audit_log", "idempotency_keys"):
            assert expected in tables, f"missing table {expected!r} after migrating to head"
    finally:
        _drop_scratch_db(dbname)


@pytest.mark.skipif(not _postgres_reachable(), reason="PostgreSQL not reachable at localhost:5432")
def test_invalid_enum_value_rejected_by_postgres_but_not_sqlite():
    """The concrete PG-2 assertion: PostgreSQL's native ENUM type rejects a
    value SQLite's plain VARCHAR column (see the same models under the
    default dev/test dialect) would silently accept."""
    dbname = _create_scratch_db()
    try:
        cfg = Config("alembic_catalog.ini")
        cfg.cmd_opts = argparse.Namespace(x=["catalog=vms"])
        with _pointed_at_catalog("vms", _pg_url(dbname)):
            command.upgrade(cfg, "head")

        assert _insert_invalid_enum_raises(dbname), (
            "PostgreSQL should reject an invalid balance_status enum value"
        )
    finally:
        _drop_scratch_db(dbname)


@pytest.mark.skipif(not _postgres_reachable(), reason="PostgreSQL not reachable at localhost:5432")
def test_downgrade_upgrade_round_trip_against_postgres_no_orphaned_type_error():
    """Agent 4's PG-2 forward-looking concern: does a downgrade/upgrade
    cycle over the FK/enum-constraint migrations hit an orphaned CREATE
    TYPE failure on re-upgrade? Answered here for real, not just reasoned
    about."""
    dbname = _create_scratch_db()
    try:
        cfg = Config("alembic_catalog.ini")
        cfg.cmd_opts = argparse.Namespace(x=["catalog=vms"])

        with _pointed_at_catalog("vms", _pg_url(dbname)):
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "-3")  # unwinds FK-ondelete, enum-constraint, version_id_col
            command.upgrade(cfg, "head")  # must not raise a duplicate/orphaned CREATE TYPE error

        tables = _table_names(dbname)
        assert "transactions" in tables
    finally:
        _drop_scratch_db(dbname)


@pytest.mark.skipif(not _postgres_reachable(), reason="PostgreSQL not reachable at localhost:5432")
def test_core_migrations_run_from_empty_against_postgres():
    dbname = _create_scratch_db()
    try:
        cfg = Config("alembic.ini")
        with _pointed_at_core(_pg_url(dbname)):
            command.upgrade(cfg, "head")

        tables = _table_names(dbname)
        assert "users" in tables
        assert "core_audit_log" in tables
    finally:
        _drop_scratch_db(dbname)
