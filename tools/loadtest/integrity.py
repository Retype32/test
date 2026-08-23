"""Post-run database reconciliation (plan section 10): the load-test-
specific subset of checks needed to populate data_integrity (section 9) and
score the acceptance criteria (section 7). Reads the SQLite files directly,
read-only, after the run's virtual users have finished -- this is
deliberately NOT a live per-request DB check (that would require a second
DB connection pool fighting the app's own under load); it is the
"cross-checked against the DB after each run" step the plan describes.

Coordinates with (but does not duplicate) Agent 4's general-purpose
integrity checker design in 04_postgresql_and_reconciliation.md -- this
module implements only the load-test-run-specific subset the plan's
section 10 lists, against SQLite specifically (this harness's target per
the plan's section 8 scope).
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from .resources import _DB_FILENAMES


def _ro_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _normalize_uuid(value: str) -> str:
    """SQLAlchemy 2.0's `Mapped[uuid.UUID]` -> `Uuid` type stores SQLite
    values as 32-char hex WITHOUT hyphens (verified empirically against a
    live-seeded DB), while every HTTP-facing rendering of the same value
    (Jinja's `{{ txn.transaction_id }}`, and pydantic's UUID JSON
    serialization) uses the standard 36-char hyphenated form. A
    harness-claimed transaction_id compared directly against the raw
    column would never match, misreporting every real commit as a missing
    record. Comparing hex-only, lowercase, on both sides makes this
    comparison correct regardless of which form either side happens to be
    in."""
    return (value or "").replace("-", "").lower()


@dataclass
class IntegrityResult:
    committed_records_by_table: dict = field(default_factory=dict)
    unique_transaction_ids: int = 0
    distinct_bag_numbers: int = 0
    duplicate_bags_found: int = 0
    missing_records: int = 0
    missing_audit_events: int = 0
    orphan_records: int = 0
    reconciliation_differences: int = 0
    unbalanced_completed_transactions: int = 0
    bag_number_cross_user_collisions: int = 0
    missing_record_ids: list = field(default_factory=list)
    missing_audit_event_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "committed_records_by_table": self.committed_records_by_table,
            "unique_transaction_ids": self.unique_transaction_ids,
            "distinct_bag_numbers": self.distinct_bag_numbers,
            "duplicate_bags_found": self.duplicate_bags_found,
            "missing_records": self.missing_records,
            "missing_audit_events": self.missing_audit_events,
            "orphan_records": self.orphan_records,
            "reconciliation_differences": self.reconciliation_differences,
        }


def _run_checks_sync(
    catalog_db_path: str,
    core_db_path: Optional[str],
    claimed_transaction_ids: list[str],
) -> IntegrityResult:
    result = IntegrityResult()
    conn = _ro_connect(catalog_db_path)
    try:
        for table in ("transactions", "denominations", "duplicate_flags", "notifications", "audit_log", "eod_closures"):
            if _table_exists(conn, table):
                result.committed_records_by_table[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            else:
                result.committed_records_by_table[table] = 0

        if _table_exists(conn, "transactions"):
            result.unique_transaction_ids = conn.execute(
                "SELECT COUNT(DISTINCT transaction_id) FROM transactions"
            ).fetchone()[0]
            result.distinct_bag_numbers = conn.execute(
                "SELECT COUNT(DISTINCT bag_number) FROM transactions"
            ).fetchone()[0]

            # Missing records: harness-claimed transaction_id not found in DB
            # (plan section 6: "should always be zero; nonzero is a critical
            # finding").
            missing = []
            for txn_id in claimed_transaction_ids:
                row = conn.execute(
                    "SELECT 1 FROM transactions WHERE REPLACE(LOWER(transaction_id), '-', '') = ?",
                    (_normalize_uuid(txn_id),),
                ).fetchone()
                if row is None:
                    missing.append(txn_id)
            result.missing_record_ids = missing
            result.missing_records = len(missing)

            # bag_number cross-user collisions (plan section 10 item 1):
            # same customer/business_date/bag_number combination appearing
            # more than once -- reported as a fact, not itself a defect
            # (the app's own detection is same-user-scoped only).
            result.bag_number_cross_user_collisions = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT customer_id, business_date, bag_number
                    FROM transactions
                    GROUP BY customer_id, business_date, bag_number
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]

            # total_value vs sum(denominations.value) -- write-consistency
            # check (plan section 10 item 2 / section 7 acceptance
            # criterion "unbalanced completed transactions").
            rows = conn.execute(
                """
                SELECT t.transaction_id, t.total_value, t.balance_status,
                       COALESCE((SELECT SUM(d.value) FROM denominations d
                                 WHERE d.transaction_id = t.transaction_id), 0) AS denom_sum
                FROM transactions t
                """
            ).fetchall()
            recon_diff = 0
            unbalanced_completed = 0
            for row in rows:
                try:
                    total_value = Decimal(str(row["total_value"]))
                    denom_sum = Decimal(str(row["denom_sum"]))
                except InvalidOperation:
                    continue
                if total_value != denom_sum:
                    recon_diff += 1
                    if row["balance_status"] == "BALANCED":
                        unbalanced_completed += 1
            result.reconciliation_differences = recon_diff
            result.unbalanced_completed_transactions = unbalanced_completed

            # Orphan denominations: no parent transaction row.
            orphan_denoms = 0
            if _table_exists(conn, "denominations"):
                orphan_denoms = conn.execute(
                    """
                    SELECT COUNT(*) FROM denominations d
                    WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.transaction_id = d.transaction_id)
                    """
                ).fetchone()[0]
            orphan_dupes = 0
            if _table_exists(conn, "duplicate_flags"):
                orphan_dupes = conn.execute(
                    """
                    SELECT COUNT(*) FROM duplicate_flags f
                    WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.transaction_id = f.transaction_id)
                       OR NOT EXISTS (SELECT 1 FROM transactions t WHERE t.transaction_id = f.duplicate_of_transaction_id)
                    """
                ).fetchone()[0]
            result.orphan_records = orphan_denoms + orphan_dupes

            # duplicate_bags_found: rows actually created in duplicate_flags.
            if _table_exists(conn, "duplicate_flags"):
                result.duplicate_bags_found = conn.execute(
                    "SELECT COUNT(*) FROM duplicate_flags"
                ).fetchone()[0]

            # Missing audit events: every claimed business-success
            # transaction create must have a matching TRANSACTION_CREATED
            # audit_log row (plan section 10 item 4). AuditLog.details is
            # free text ("Transaction {id} created for customer ..."), so
            # this is a substring match, not a foreign key -- the closest
            # this schema supports without a dedicated audit-search index.
            missing_audit = []
            if _table_exists(conn, "audit_log"):
                for txn_id in claimed_transaction_ids:
                    if txn_id in result.missing_record_ids:
                        continue  # already counted as missing_records
                    row = conn.execute(
                        "SELECT 1 FROM audit_log WHERE action = 'TRANSACTION_CREATED' AND details LIKE ?",
                        (f"%{txn_id}%",),
                    ).fetchone()
                    if row is None:
                        missing_audit.append(txn_id)
            result.missing_audit_event_ids = missing_audit
            result.missing_audit_events = len(missing_audit)
    finally:
        conn.close()

    if core_db_path:
        try:
            core_conn = _ro_connect(core_db_path)
            try:
                if _table_exists(core_conn, "core_audit_log"):
                    result.committed_records_by_table["core_audit_log"] = core_conn.execute(
                        "SELECT COUNT(*) FROM core_audit_log"
                    ).fetchone()[0]
            finally:
                core_conn.close()
        except sqlite3.OperationalError:
            result.committed_records_by_table.setdefault("core_audit_log", 0)

    return result


async def run_integrity_checks(
    catalog_db_path: str,
    core_db_path: Optional[str],
    claimed_transaction_ids: list[str],
) -> IntegrityResult:
    """Async wrapper -- the sqlite3 calls are synchronous/fast (local file,
    read-only, runs once after the load window closes), offloaded to a
    thread purely so callers in the async CLI don't need a separate sync
    entrypoint."""
    return await asyncio.to_thread(
        _run_checks_sync, catalog_db_path, core_db_path, claimed_transaction_ids
    )


def catalog_db_path(db_dir: str, catalog: str) -> str:
    import os
    return os.path.join(db_dir, _DB_FILENAMES[catalog])


def core_db_path(db_dir: str) -> str:
    import os
    return os.path.join(db_dir, _DB_FILENAMES["core"])
