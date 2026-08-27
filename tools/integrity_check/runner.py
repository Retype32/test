"""Orchestrates one catalog's checks: opens read-only connections, computes
scope.rows_scanned, runs the requested checks, and assembles a CatalogReport.
"""
from __future__ import annotations

import contextlib
import time
from typing import Optional

from sqlalchemy import func, select

from backend.models.duplicate import DuplicateFlag
from backend.models.eod import EODClosure
from backend.models.notification import Notification
from backend.models.transaction import Denomination, Transaction

from .checks import ALL_CHECK_IDS, CHECK_BY_ID, NOT_APPLICABLE
from .db import RunContext, open_readonly_connection
from .results import SEVERITY_ORDER, CatalogReport, CheckResult


def expand_check_ids(requested: str) -> list[str]:
    if requested == "all":
        return list(ALL_CHECK_IDS)
    ids = [c.strip() for c in requested.split(",") if c.strip()]
    unknown = [i for i in ids if i not in ALL_CHECK_IDS]
    if unknown:
        raise ValueError(f"unknown check id(s): {', '.join(unknown)}")
    return ids


async def compute_rows_scanned(ctx: RunContext) -> dict:
    conn = ctx.catalog_conn

    txn_stmt = select(func.count()).select_from(Transaction.__table__)
    for c in ctx.date_clauses(Transaction.business_date):
        txn_stmt = txn_stmt.where(c)
    transactions = (await conn.execute(txn_stmt)).scalar_one()

    denom_stmt = select(func.count()).select_from(Denomination.__table__)
    if ctx.date_from is not None or ctx.date_to is not None:
        denom_stmt = select(func.count()).select_from(
            Denomination.__table__.join(Transaction.__table__, Denomination.transaction_id == Transaction.transaction_id)
        )
        for c in ctx.date_clauses(Transaction.business_date):
            denom_stmt = denom_stmt.where(c)
    denominations = (await conn.execute(denom_stmt)).scalar_one()

    eod_stmt = select(func.count()).select_from(EODClosure.__table__)
    for c in ctx.date_clauses(EODClosure.business_date):
        eod_stmt = eod_stmt.where(c)
    eod_closures = (await conn.execute(eod_stmt)).scalar_one()

    # duplicate_flags/notifications carry no business_date column of their
    # own (detected_at/created_at are timestamps, not the business concept)
    # -- counted in full regardless of --business-date-from/to.
    duplicate_flags = (await conn.execute(select(func.count()).select_from(DuplicateFlag.__table__))).scalar_one()
    notifications = (await conn.execute(select(func.count()).select_from(Notification.__table__))).scalar_one()

    return {
        "transactions": transactions,
        "denominations": denominations,
        "eod_closures": eod_closures,
        "duplicate_flags": duplicate_flags,
        "notifications": notifications,
    }


async def run_catalog(
    *,
    catalog_code: str,
    catalog_url: str,
    catalog_url_redacted: str,
    core_url: Optional[str],
    core_url_redacted: Optional[str],
    check_ids: list[str],
    severity_threshold: str,
    date_from,
    date_to,
    sample_size: int,
) -> CatalogReport:
    async with contextlib.AsyncExitStack() as stack:
        catalog_conn = await stack.enter_async_context(open_readonly_connection(catalog_url))

        core_conn = None
        needs_core = any(CHECK_BY_ID[cid].needs_core for cid in check_ids if cid in CHECK_BY_ID)
        # restricted_actions_without_approval degrades gracefully without
        # core (catalog-side-only); records_attributed_... hard-requires it
        # (needs_core=True) -- only open the core connection when something
        # requested actually needs or can use it, and core_url is resolvable.
        wants_core = needs_core or (
            "restricted_actions_without_approval" in check_ids and core_url is not None
        )
        if wants_core and core_url is not None:
            core_conn = await stack.enter_async_context(open_readonly_connection(core_url))

        ctx = RunContext(
            catalog_conn=catalog_conn,
            core_conn=core_conn,
            catalog_code=catalog_code,
            date_from=date_from,
            date_to=date_to,
            sample_size=sample_size,
        )

        rows_scanned = await compute_rows_scanned(ctx)

        checks_run = []
        checks_not_applicable = []
        results = []
        for cid in check_ids:
            if cid in NOT_APPLICABLE:
                checks_not_applicable.append({"check_id": cid, "reason": NOT_APPLICABLE[cid]})
                continue
            spec = CHECK_BY_ID[cid]
            if spec.needs_core and core_conn is None:
                checks_not_applicable.append(
                    {
                        "check_id": cid,
                        "reason": "no --core-database-url resolvable (explicit flag, DATABASE_URL_CORE env, "
                        "or app default) -- required for this cross-database check",
                    }
                )
                continue
            start = time.perf_counter()
            outcome = await spec.fn(ctx)
            duration_ms = int((time.perf_counter() - start) * 1000)
            checks_run.append(cid)
            results.append(
                CheckResult(
                    check_id=cid,
                    description=spec.description,
                    severity=spec.severity,
                    status="pass" if outcome.violation_count == 0 else "fail",
                    violation_count=outcome.violation_count,
                    query_duration_ms=duration_ms,
                    sample_violations=outcome.sample_violations,
                    extra=outcome.extra,
                )
            )

        threshold_rank = SEVERITY_ORDER[severity_threshold]
        exit_code = 0
        for r in results:
            if SEVERITY_ORDER[r.severity] >= threshold_rank and r.status == "fail":
                exit_code = 1
                break

        return CatalogReport(
            catalog=catalog_code,
            catalog_database_url_redacted=catalog_url_redacted,
            core_database_url_redacted=core_url_redacted if core_conn is not None else None,
            business_date_from=date_from.isoformat() if date_from else None,
            business_date_to=date_to.isoformat() if date_to else None,
            rows_scanned=rows_scanned,
            checks_run=checks_run,
            checks_not_applicable=checks_not_applicable,
            results=results,
            exit_code=exit_code,
        )
