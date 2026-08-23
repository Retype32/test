"""Backup/restore round-trip tests for PG-6.

There was no restore path anywhere in the codebase before this change, and
the SQLite-only backup path had never been proven restorable either. This
module proves both, end to end, for both dialects:

1. SQLite: scratch temp-directory .db files only (tmp_path fixture) --
   never the real dev .db files (confirmed absent from the repo before
   writing this test: `find . -name "*.db"` returns nothing).
2. PostgreSQL: real databases on the sandbox's live PostgreSQL 16 server
   (localhost:5432, nexus_test role). Deliberately does NOT write schema or
   data into the 5 shared nexus_*_test databases the task description
   pre-created -- two other agents are running in parallel against the same
   PostgreSQL server and may be exercising those exact databases for their
   own real work right now, and this test needs to create tables, insert
   rows, and (for the "clean environment" target) restore into a
   from-scratch database, none of which should risk colliding with that
   work. Instead it creates its own disposable `nexus_dbops_*` databases via
   `CREATE DATABASE` (the nexus_test role has CREATEDB, confirmed by the
   task) and drops them in a `finally` block. This is a deliberate,
   documented deviation from doing it literally "in" nexus_core_test /
   nexus_vms_test -- same server, same role, same pg_dump/pg_restore code
   path, just isolated database names so a second concurrent agent's test
   run can never see or be affected by this one's data.

Both round trips independently prove the same thing required by the task:
create schema -> insert representative rows -> back up -> land in a clean
environment -> restore -> assert the data matches.
"""
import os
import shutil
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.core.database import CoreBase, CatalogBase
from backend.models import User, UserRole, Customer, Location, Transaction, Denomination
from backend.services import backup_service, restore_service

PG_HOST = "localhost"
PG_PORT = 5432
PG_USER = "nexus_test"
PG_PASSWORD = "nexus_test_pw_2026"


def _pg_url(dbname: str) -> str:
    return f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"


def _postgres_reachable() -> bool:
    import socket
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=2):
            return True
    except OSError:
        return False


async def _admin_conn():
    import asyncpg
    return await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database="postgres",
    )


async def _create_database(name: str) -> None:
    conn = await _admin_conn()
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(name: str) -> None:
    conn = await _admin_conn()
    try:
        # Terminate any lingering backends first -- DROP DATABASE fails if
        # anything (e.g. a pool connection this test forgot to dispose) is
        # still connected.
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await conn.close()


async def _seed_core(core_url: str, user_id: uuid.UUID, username: str) -> None:
    engine = create_async_engine(core_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(CoreBase.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(User(
                id=user_id, username=username, password_hash="not-a-real-hash",
                role=UserRole.cashier,
            ))
            await s.commit()
    finally:
        await engine.dispose()


async def _seed_catalog(catalog_url: str, txn_id: uuid.UUID, bag_number: str) -> uuid.UUID:
    engine = create_async_engine(catalog_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(CatalogBase.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        user_id = uuid.uuid4()
        async with Session() as s:
            s.add(Customer(customer_id="CUST-DBOPS", customer_name="DB-OPS Test Customer"))
            s.add(Location(location_id="LOC-DBOPS", customer_id="CUST-DBOPS", location_name="DB-OPS Test Location"))
            await s.flush()
            s.add(Transaction(
                transaction_id=txn_id, user_id=user_id, username="dbops_tester",
                customer_id="CUST-DBOPS", location_id="LOC-DBOPS", bag_number=bag_number,
                total_value=Decimal("125.50"),
            ))
            await s.flush()
            s.add(Denomination(transaction_id=txn_id, denomination="20", count=5, value=Decimal("100.00")))
            s.add(Denomination(transaction_id=txn_id, denomination="10", count=2, value=Decimal("25.50")))
            await s.commit()
        return user_id
    finally:
        await engine.dispose()


async def _assert_core_has_user(core_url: str, user_id: uuid.UUID, expected_username: str) -> None:
    engine = create_async_engine(core_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            row = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
            assert row.username == expected_username
    finally:
        await engine.dispose()


async def _assert_catalog_has_transaction(
    catalog_url: str, txn_id: uuid.UUID, expected_bag_number: str,
) -> None:
    engine = create_async_engine(catalog_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            txn = (await s.execute(
                select(Transaction).where(Transaction.transaction_id == txn_id)
            )).scalar_one()
            assert txn.bag_number == expected_bag_number
            assert txn.total_value == Decimal("125.50")

            denoms = (await s.execute(
                select(Denomination).where(Denomination.transaction_id == txn_id)
            )).scalars().all()
            assert {(d.denomination, d.count) for d in denoms} == {("20", 5), ("10", 2)}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# SQLite round trip
# ---------------------------------------------------------------------------

async def test_sqlite_backup_restore_round_trip(tmp_path):
    """Backs up scratch SQLite databases, then deliberately corrupts the
    live files (deletes the seeded rows) to prove the restore actually
    brings the original data back -- not just that files exist afterward.
    Exercises the documented precondition directly: the live engine is
    disposed before the restore overwrites its file."""
    core_url = f"sqlite+aiosqlite:///{tmp_path}/core.db"
    catalog_url = f"sqlite+aiosqlite:///{tmp_path}/catalog_x.db"

    user_id = uuid.uuid4()
    txn_id = uuid.uuid4()
    await _seed_core(core_url, user_id, "sqlite_dbops_tester")
    await _seed_catalog(catalog_url, txn_id, "SQLITE-BAG-1")
    # _seed_* disposes its own engine before returning -- backup_service's
    # sqlite3.Connection.backup() call needs no live aiosqlite handle held.

    backup_result = await backup_service.create_backup(sources={
        "core": core_url, "catalog_x": catalog_url,
    })
    try:
        assert backup_result["failed"] == [], backup_result["failed"]
        assert set(backup_result["copied"]) == {"core", "catalog_x"}
        assert os.path.exists(os.path.join(backup_result["dir"], "core.db"))
        assert os.path.exists(os.path.join(backup_result["dir"], "catalog_x.db"))

        # Corrupt the live databases (delete the very rows we just backed up)
        # so a passing restore assertion can only mean the backup file's
        # original contents actually came back, not that the row was never
        # touched.
        corrupt_engine = create_async_engine(core_url)
        async with async_sessionmaker(corrupt_engine, expire_on_commit=False)() as s:
            await s.execute(User.__table__.delete())
            await s.commit()
        await corrupt_engine.dispose()

        corrupt_catalog_engine = create_async_engine(catalog_url)
        async with async_sessionmaker(corrupt_catalog_engine, expire_on_commit=False)() as s:
            await s.execute(Denomination.__table__.delete())
            await s.execute(Transaction.__table__.delete())
            await s.commit()
        await corrupt_catalog_engine.dispose()  # precondition: dispose before restore

        restore_result = await restore_service.restore_backup(backup_result["dir"], targets={
            "core": core_url, "catalog_x": catalog_url,
        })
        assert restore_result["failed"] == [], restore_result["failed"]
        assert set(restore_result["restored"]) == {"core", "catalog_x"}

        await _assert_core_has_user(core_url, user_id, "sqlite_dbops_tester")
        await _assert_catalog_has_transaction(catalog_url, txn_id, "SQLITE-BAG-1")
    finally:
        # BACKUP_ROOT is the real (gitignored) repo backups/ directory, not
        # tmp_path -- clean up what this test wrote so repeated runs don't
        # pile up directories there (mirrors test_new_features.py's existing
        # admin-backup test's own cleanup of the same directory).
        shutil.rmtree(backup_result["dir"], ignore_errors=True)


# ---------------------------------------------------------------------------
# PostgreSQL round trip
# ---------------------------------------------------------------------------

pg_skip = pytest.mark.skipif(
    not _postgres_reachable(), reason=f"PostgreSQL not reachable on {PG_HOST}:{PG_PORT}",
)


@pg_skip
async def test_postgres_backup_restore_round_trip():
    """Full PG-6 round trip against real PostgreSQL: create schema, insert
    representative rows, pg_dump, land in a brand-new ('clean environment')
    database via CREATE DATABASE, pg_restore, assert the data matches."""
    suffix = uuid.uuid4().hex[:10]
    src_core = f"nexus_dbops_src_core_{suffix}"
    src_catalog = f"nexus_dbops_src_catalog_{suffix}"
    dst_core = f"nexus_dbops_dst_core_{suffix}"
    dst_catalog = f"nexus_dbops_dst_catalog_{suffix}"
    all_dbs = [src_core, src_catalog, dst_core, dst_catalog]

    for name in all_dbs:
        await _create_database(name)

    backup_result = None
    try:
        user_id = uuid.uuid4()
        txn_id = uuid.uuid4()
        await _seed_core(_pg_url(src_core), user_id, "pg_dbops_tester")
        await _seed_catalog(_pg_url(src_catalog), txn_id, "PG-BAG-1")

        backup_result = await backup_service.create_backup(sources={
            "core": _pg_url(src_core), "catalog_x": _pg_url(src_catalog),
        })
        assert backup_result["failed"] == [], backup_result["failed"]
        assert set(backup_result["copied"]) == {"core", "catalog_x"}
        assert os.path.exists(os.path.join(backup_result["dir"], "core.dump"))
        assert os.path.exists(os.path.join(backup_result["dir"], "catalog_x.dump"))

        restore_result = await restore_service.restore_backup(backup_result["dir"], targets={
            "core": _pg_url(dst_core), "catalog_x": _pg_url(dst_catalog),
        })
        assert restore_result["failed"] == [], restore_result["failed"]
        assert set(restore_result["restored"]) == {"core", "catalog_x"}

        await _assert_core_has_user(_pg_url(dst_core), user_id, "pg_dbops_tester")
        await _assert_catalog_has_transaction(_pg_url(dst_catalog), txn_id, "PG-BAG-1")
    finally:
        if backup_result is not None:
            shutil.rmtree(backup_result["dir"], ignore_errors=True)
        for name in all_dbs:
            await _drop_database(name)


# ---------------------------------------------------------------------------
# Per-database error isolation (Agent 5 finding #12)
# ---------------------------------------------------------------------------

async def test_create_backup_continues_past_a_failing_database(tmp_path):
    """One database's backup failure must not abort the rest of the run,
    and must be reported structurally rather than swallowed."""
    good_url = f"sqlite+aiosqlite:///{tmp_path}/good.db"
    await _seed_core(good_url, uuid.uuid4(), "isolation_test_user")

    bad_url = "sqlite+aiosqlite:///not-a-real-directory-xyz/does_not_exist.db"

    result = await backup_service.create_backup(sources={"good": good_url, "bad": bad_url})
    try:
        assert result["copied"] == ["good"]
        assert result["skipped"] == ["bad"]  # source file doesn't exist -> skipped, not a hard failure
        assert os.path.exists(os.path.join(result["dir"], "good.db"))
    finally:
        shutil.rmtree(result["dir"], ignore_errors=True)


async def test_create_backup_records_structured_failure_not_just_success(tmp_path, monkeypatch):
    """Force an actual backup-time failure (not a missing-source skip) on
    one database and confirm it lands in `failed` with an error message,
    while a sibling database in the same run still succeeds."""
    good_url = f"sqlite+aiosqlite:///{tmp_path}/good2.db"
    await _seed_core(good_url, uuid.uuid4(), "isolation_test_user_2")

    bad_url = f"sqlite+aiosqlite:///{tmp_path}/bad2.db"
    # A file exists at the path but isn't a valid SQLite database -- backup
    # will pass the os.path.exists() check and then fail inside sqlite3's
    # backup API itself.
    with open(f"{tmp_path}/bad2.db", "wb") as f:
        f.write(b"not a sqlite database")

    result = await backup_service.create_backup(sources={"good": good_url, "bad": bad_url})
    try:
        assert result["copied"] == ["good"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["name"] == "bad"
        assert result["failed"][0]["error"]  # non-empty message, not swallowed
        assert os.path.exists(os.path.join(result["dir"], "good.db"))
    finally:
        shutil.rmtree(result["dir"], ignore_errors=True)
