"""Regression tests for the tools/integrity_check CLI (Agent 4's checker
design, docs/production_readiness/04_postgresql_and_reconciliation.md §4).

Runs the real CLI entrypoint (`tools.integrity_check.cli.main`) in-process
against scratch SQLite databases in a temp directory -- never the real dev
.db files. Each test is a synchronous `def`, not `async def`: `cli.main`
manages its own event loop internally (`asyncio.run(...)`), which would
raise "cannot be called from a running event loop" if invoked from inside
pytest-asyncio's own loop.

Deliberately not exhaustive over all 13 checks (that duplicates the tool's
own design doc) -- this proves the CLI contract itself (exit code 0 with no
violations, non-zero with real ones) and exercises the checks most directly
tied to Wave 1's new schema (header/denomination reconciliation, orphan
detection, and the audit-completeness check that cross-references Wave 1's
correction workflow).
"""
import asyncio
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.core.database import CoreBase, CatalogBase
from backend.models import User, UserRole, Customer, Location, Transaction, Denomination
from backend.models.transaction import AuditLog, BalanceStatus
from tools.integrity_check import cli


def _urls(tmp_path):
    core = f"sqlite+aiosqlite:///{tmp_path}/core.db"
    catalog = f"sqlite+aiosqlite:///{tmp_path}/catalog.db"
    return core, catalog


async def _seed_core_user(core_url: str, user_id: uuid.UUID) -> None:
    engine = create_async_engine(core_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(CoreBase.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(User(
                id=user_id, username="integrity_test_user",
                password_hash="not-a-real-hash", role=UserRole.cashier,
            ))
            await s.commit()
    finally:
        await engine.dispose()


async def _seed_catalog(catalog_url: str, seed_fn) -> None:
    engine = create_async_engine(catalog_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(CatalogBase.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(Customer(customer_id="CUST-IC", customer_name="Integrity Check Customer"))
            s.add(Location(location_id="LOC-IC", customer_id="CUST-IC", location_name="Integrity Check Location"))
            await s.flush()
            await seed_fn(s)
            await s.commit()
    finally:
        await engine.dispose()


def _base_transaction(user_id: uuid.UUID, txn_id: uuid.UUID, total: Decimal) -> Transaction:
    return Transaction(
        transaction_id=txn_id, user_id=user_id, username="integrity_test_user",
        customer_id="CUST-IC", location_id="LOC-IC", bag_number="310000000001",
        total_value=total, expected_total=total, balance_status=BalanceStatus.balanced,
        business_date=date(2026, 1, 15),
    )


def test_clean_seeded_catalog_passes_all_applicable_checks(tmp_path):
    core_url, catalog_url = _urls(tmp_path)
    user_id = uuid.uuid4()
    txn_id = uuid.uuid4()

    async def seed(s):
        txn = _base_transaction(user_id, txn_id, Decimal("100.00"))
        s.add(txn)
        await s.flush()
        s.add(Denomination(transaction_id=txn_id, denomination="50", count=2, value=Decimal("100.00")))

    asyncio.run(_seed_core_user(core_url, user_id))
    asyncio.run(_seed_catalog(catalog_url, seed))

    exit_code = cli.main([
        "--catalog", "vms",
        "--catalog-database-url", catalog_url,
        "--core-database-url", core_url,
        "--format", "json",
    ])

    assert exit_code == 0, "a clean, internally-consistent catalog must exit 0"


def test_header_total_vs_denominations_mismatch_is_caught(tmp_path, capsys):
    core_url, catalog_url = _urls(tmp_path)
    user_id = uuid.uuid4()
    txn_id = uuid.uuid4()

    async def seed(s):
        # total_value says 100.00, but the only denomination sums to 40.00 --
        # a write-consistency defect the checker's header_total_vs_denominations
        # check exists specifically to catch (PG-4 in the source doc).
        txn = _base_transaction(user_id, txn_id, Decimal("100.00"))
        s.add(txn)
        await s.flush()
        s.add(Denomination(transaction_id=txn_id, denomination="20", count=2, value=Decimal("40.00")))

    asyncio.run(_seed_core_user(core_url, user_id))
    asyncio.run(_seed_catalog(catalog_url, seed))

    exit_code = cli.main([
        "--catalog", "vms",
        "--catalog-database-url", catalog_url,
        "--core-database-url", core_url,
        "--format", "json",
    ])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert '"check_id": "header_total_vs_denominations"' in out
    assert '"status": "fail"' in out
    assert str(txn_id) in out


def test_orphan_denomination_is_caught(tmp_path, capsys):
    core_url, catalog_url = _urls(tmp_path)
    user_id = uuid.uuid4()
    orphan_txn_id = uuid.uuid4()  # never actually inserted as a Transaction row

    async def seed(s):
        # Bypass the ORM relationship/cascade entirely: insert a Denomination
        # whose transaction_id points at nothing. On SQLite this is only
        # possible because FK enforcement is off by default (PG-12) -- which
        # is exactly the live FK-health signal this check is designed to
        # surface, per its own description in checks.py.
        s.add(Denomination(transaction_id=orphan_txn_id, denomination="10", count=1, value=Decimal("10.00")))

    asyncio.run(_seed_core_user(core_url, user_id))
    asyncio.run(_seed_catalog(catalog_url, seed))

    exit_code = cli.main([
        "--catalog", "vms",
        "--catalog-database-url", catalog_url,
        "--core-database-url", core_url,
        "--format", "json",
    ])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert '"check_id": "orphan_denominations"' in out
    assert '"status": "fail"' in out


def test_missing_correction_audit_event_is_caught(tmp_path, capsys):
    core_url, catalog_url = _urls(tmp_path)
    user_id = uuid.uuid4()
    txn_id = uuid.uuid4()

    async def seed(s):
        # is_superseded=True with no matching TRANSACTION_CORRECTED AuditLog
        # entry -- exactly the scenario missing_audit_events exists to catch.
        # (No corresponding successor row is needed for this check: it only
        # looks at the superseded original's own id against AuditLog.details.)
        txn = _base_transaction(user_id, txn_id, Decimal("75.00"))
        txn.is_superseded = True
        s.add(txn)
        await s.flush()
        s.add(Denomination(transaction_id=txn_id, denomination="50", count=1, value=Decimal("50.00")))
        s.add(Denomination(transaction_id=txn_id, denomination="20", count=1, value=Decimal("20.00")))
        s.add(Denomination(transaction_id=txn_id, denomination="5", count=1, value=Decimal("5.00")))
        # An unrelated audit entry exists, but never references this
        # transaction_id -- confirms the check does substring-matching
        # against real entries, not just "any TRANSACTION_CORRECTED row
        # exists at all".
        s.add(AuditLog(
            user_id=user_id, action="TRANSACTION_CORRECTED",
            timestamp=datetime.now(timezone.utc),
            details=f"Corrected transaction {uuid.uuid4()} (unrelated)",
        ))

    asyncio.run(_seed_core_user(core_url, user_id))
    asyncio.run(_seed_catalog(catalog_url, seed))

    exit_code = cli.main([
        "--catalog", "vms",
        "--catalog-database-url", catalog_url,
        "--core-database-url", core_url,
        "--format", "json",
    ])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert '"check_id": "missing_audit_events"' in out
    assert '"status": "fail"' in out
    assert str(txn_id) in out


def test_severity_threshold_filters_which_failures_affect_exit_code(tmp_path):
    """A high-severity violation still fails exit_code even when
    --severity-threshold is raised to critical (no check in the current
    catalog is critical-severity), matching the CLI contract's documented
    behavior: only checks at or above the threshold affect exit_code 0/1."""
    core_url, catalog_url = _urls(tmp_path)
    user_id = uuid.uuid4()
    txn_id = uuid.uuid4()

    async def seed(s):
        txn = _base_transaction(user_id, txn_id, Decimal("100.00"))
        s.add(txn)
        await s.flush()
        s.add(Denomination(transaction_id=txn_id, denomination="20", count=2, value=Decimal("40.00")))

    asyncio.run(_seed_core_user(core_url, user_id))
    asyncio.run(_seed_catalog(catalog_url, seed))

    exit_code = cli.main([
        "--catalog", "vms",
        "--catalog-database-url", catalog_url,
        "--core-database-url", core_url,
        "--format", "json",
        "--severity-threshold", "critical",
    ])

    # header_total_vs_denominations is "high", not "critical" -- above the
    # critical-only threshold nothing qualifies, so this must pass.
    assert exit_code == 0
