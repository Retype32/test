"""The check catalog. Every function here is `async def fn(ctx) -> CheckOutcome`,
issues SELECT-only statements against `ctx.catalog_conn`/`ctx.core_conn`
(plain SQLAlchemy Core `AsyncConnection`s -- see db.py), and never touches a
Session, so there is nothing here with a `.add()`/`.commit()` to call in the
first place.

Each check is registered in CHECK_CATALOG with its severity and description,
mirroring docs/production_readiness/04_postgresql_and_reconciliation.md
§4.4's table. `NOT_APPLICABLE` mirrors §5's exclusions -- kept current for
Wave 1: `duplicate_idempotency_keys` moved OUT of this list (the
`idempotency_keys` table now exists) and is implemented below instead.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import and_, func, literal, or_, select

from backend.models.core_audit import CoreAuditLog
from backend.models.duplicate import DuplicateFlag, DuplicateFlagStatus
from backend.models.eod import EODClosure
from backend.models.idempotency import IdempotencyKey
from backend.models.notification import Notification
from backend.models.transaction import AuditLog, BalanceStatus, Denomination, Transaction
from backend.models.user import User

from .db import RunContext
from .results import CheckOutcome

VALID_DUPLICATE_FLAG_STATUSES = {"none", "pending_review", "confirmed_duplicate", "dismissed"}

# Sensitive AuditLog (catalog DB) action types -- restricted_actions_without_approval.
SENSITIVE_CATALOG_ACTIONS = ("TRANSACTION_DAY_TRANSFERRED", "EOD_REOPENED")
# Sensitive CoreAuditLog (core DB) action types.
SENSITIVE_CORE_ACTIONS = ("USER_DELETED", "USER_UPDATED")


def _cap(rows: list, n: int) -> list:
    return rows[:n]


# ---------------------------------------------------------------------------
# duplicate_bag_numbers
# ---------------------------------------------------------------------------

async def check_duplicate_bag_numbers(ctx: RunContext) -> CheckOutcome:
    norm_bag = func.lower(func.trim(Transaction.bag_number))
    stmt = (
        select(
            Transaction.customer_id,
            Transaction.location_id,
            Transaction.business_date,
            norm_bag.label("norm_bag"),
            func.count(Transaction.transaction_id).label("cnt"),
        )
        .group_by(Transaction.customer_id, Transaction.location_id, Transaction.business_date, norm_bag)
        .having(func.count(Transaction.transaction_id) > 1)
    )
    for c in ctx.date_clauses(Transaction.business_date):
        stmt = stmt.where(c)
    groups = (await ctx.catalog_conn.execute(stmt)).all()

    samples = []
    for g in _cap(groups, ctx.sample_size):
        txn_stmt = select(Transaction.transaction_id, Transaction.username, Transaction.bag_number).where(
            Transaction.customer_id == g.customer_id,
            Transaction.location_id == g.location_id,
            Transaction.business_date == g.business_date,
            norm_bag == g.norm_bag,
        )
        txns = (await ctx.catalog_conn.execute(txn_stmt)).all()
        samples.append(
            {
                "customer_id": g.customer_id,
                "location_id": g.location_id,
                "business_date": g.business_date,
                "bag_number": g.norm_bag,
                "count": g.cnt,
                "transaction_ids": [str(t.transaction_id) for t in txns],
                "usernames": sorted({t.username for t in txns}),
            }
        )
    return CheckOutcome(violation_count=len(groups), sample_violations=samples)


# ---------------------------------------------------------------------------
# duplicate_idempotency_keys (Wave-1-unlocked -- see module docstring)
# ---------------------------------------------------------------------------

async def check_duplicate_idempotency_keys(ctx: RunContext) -> CheckOutcome:
    # `key` is the table's primary key, so a literal SQL-level duplicate key
    # is structurally impossible -- this instead verifies the two ways the
    # idempotency *logic* itself could be broken while every key stays
    # unique:
    orphan_stmt = select(IdempotencyKey.key, IdempotencyKey.scope, IdempotencyKey.transaction_id).where(
        IdempotencyKey.transaction_id.isnot(None),
        IdempotencyKey.transaction_id.notin_(select(Transaction.transaction_id)),
    )
    orphans = (await ctx.catalog_conn.execute(orphan_stmt)).all()

    dup_stmt = (
        select(IdempotencyKey.scope, IdempotencyKey.transaction_id, func.count(IdempotencyKey.key).label("cnt"))
        .where(IdempotencyKey.transaction_id.isnot(None))
        .group_by(IdempotencyKey.scope, IdempotencyKey.transaction_id)
        .having(func.count(IdempotencyKey.key) > 1)
    )
    dup_groups = (await ctx.catalog_conn.execute(dup_stmt)).all()

    samples = []
    for o in _cap(orphans, ctx.sample_size):
        samples.append(
            {
                "reason": "references a transaction_id that does not exist",
                "key": o.key,
                "scope": o.scope,
                "transaction_id": str(o.transaction_id),
            }
        )
    remaining = max(0, ctx.sample_size - len(samples))
    for d in _cap(dup_groups, remaining):
        keys_stmt = select(IdempotencyKey.key).where(
            IdempotencyKey.scope == d.scope, IdempotencyKey.transaction_id == d.transaction_id
        )
        keys = [r[0] for r in (await ctx.catalog_conn.execute(keys_stmt)).all()]
        samples.append(
            {
                "reason": "two different idempotency keys resolved to the same transaction_id within the same scope",
                "scope": d.scope,
                "transaction_id": str(d.transaction_id),
                "keys": keys,
            }
        )
    return CheckOutcome(violation_count=len(orphans) + len(dup_groups), sample_violations=samples)


# ---------------------------------------------------------------------------
# orphan_denominations
# ---------------------------------------------------------------------------

async def check_orphan_denominations(ctx: RunContext) -> CheckOutcome:
    stmt = select(
        Denomination.id, Denomination.transaction_id, Denomination.denomination, Denomination.value
    ).where(Denomination.transaction_id.notin_(select(Transaction.transaction_id)))
    rows = (await ctx.catalog_conn.execute(stmt)).all()
    samples = [
        {
            "denomination_id": str(r.id),
            "transaction_id": str(r.transaction_id),
            "denomination": r.denomination,
            "value": r.value,
        }
        for r in _cap(rows, ctx.sample_size)
    ]
    return CheckOutcome(violation_count=len(rows), sample_violations=samples)


# ---------------------------------------------------------------------------
# missing_audit_events
# ---------------------------------------------------------------------------

async def check_missing_audit_events(ctx: RunContext) -> CheckOutcome:
    # Cross-references AuditLog for the two event types the codebase
    # actually emits and that this check can verify unambiguously by
    # `transaction_id`/`business_date` substring matching against
    # AuditLog.details (free text, not structured -- see
    # transaction_service.py/eod_service.py for the exact detail strings
    # matched below).
    #
    # Deviation from the source doc's illustrative "every core User created
    # must have a USER_CREATED-family entry" example: confirmed by reading
    # backend/services/auth_service.py that user creation is NEVER audited
    # in this codebase (create_user has no audit_repo.log call at all,
    # unlike update/delete/activate which do). Checking for a USER_CREATED
    # entry would therefore flag every single user as a violation --
    # 100% false positives, not evidence of a real defect -- so that sub-
    # check is intentionally omitted here rather than force-implemented.
    stmt = select(Transaction.transaction_id).where(Transaction.is_superseded.is_(True))
    for c in ctx.date_clauses(Transaction.business_date):
        stmt = stmt.where(c)
    superseded_ids = [r[0] for r in (await ctx.catalog_conn.execute(stmt)).all()]

    corrected_logged = []
    if superseded_ids:
        logged_stmt = select(AuditLog.details).where(AuditLog.action == "TRANSACTION_CORRECTED")
        corrected_logged = [d or "" for (d,) in (await ctx.catalog_conn.execute(logged_stmt)).all()]

    missing_corrected = [
        tid for tid in superseded_ids if not any(str(tid) in d for d in corrected_logged)
    ]

    eod_stmt = select(EODClosure.business_date)
    for c in ctx.date_clauses(EODClosure.business_date):
        eod_stmt = eod_stmt.where(c)
    eod_dates = [r[0] for r in (await ctx.catalog_conn.execute(eod_stmt)).all()]

    eod_logged = []
    if eod_dates:
        logged_stmt = select(AuditLog.details).where(AuditLog.action == "EOD_CLOSED")
        eod_logged = [d or "" for (d,) in (await ctx.catalog_conn.execute(logged_stmt)).all()]

    missing_eod = [
        d for d in eod_dates if not any(f"business_date={d}" in det for det in eod_logged)
    ]

    samples = [
        {
            "entity": "transaction",
            "transaction_id": str(tid),
            "reason": "is_superseded=True but no TRANSACTION_CORRECTED audit entry references it",
        }
        for tid in _cap(missing_corrected, ctx.sample_size)
    ]
    remaining = max(0, ctx.sample_size - len(samples))
    samples += [
        {
            "entity": "eod_closure",
            "business_date": d,
            "reason": "closure exists but no EOD_CLOSED audit entry references it",
        }
        for d in _cap(missing_eod, remaining)
    ]
    return CheckOutcome(violation_count=len(missing_corrected) + len(missing_eod), sample_violations=samples)


# ---------------------------------------------------------------------------
# invalid_state_transitions
# ---------------------------------------------------------------------------

async def check_invalid_state_transitions(ctx: RunContext) -> CheckOutcome:
    stmt = select(
        Transaction.transaction_id,
        Transaction.duplicate_flag_status,
        Transaction.balance_status,
        Transaction.expected_total,
        Transaction.total_value,
        Transaction.is_superseded,
        Transaction.original_transaction_id,
        Transaction.business_date,
    ).where(
        or_(
            Transaction.duplicate_flag_status.notin_(VALID_DUPLICATE_FLAG_STATUSES),
            and_(Transaction.expected_total.is_(None), Transaction.balance_status != BalanceStatus.pending),
            and_(
                Transaction.expected_total.isnot(None),
                Transaction.total_value == Transaction.expected_total,
                Transaction.balance_status != BalanceStatus.balanced,
            ),
            and_(
                Transaction.expected_total.isnot(None),
                Transaction.total_value != Transaction.expected_total,
                Transaction.balance_status != BalanceStatus.not_balanced,
            ),
            and_(Transaction.is_superseded.is_(True), Transaction.original_transaction_id.isnot(None)),
        )
    )
    for c in ctx.date_clauses(Transaction.business_date):
        stmt = stmt.where(c)
    rows = (await ctx.catalog_conn.execute(stmt)).all()

    def _reason(r) -> str:
        reasons = []
        if r.duplicate_flag_status not in VALID_DUPLICATE_FLAG_STATUSES:
            reasons.append(f"duplicate_flag_status={r.duplicate_flag_status!r} outside known vocabulary")
        if r.expected_total is None and r.balance_status != BalanceStatus.pending:
            reasons.append("expected_total is NULL but balance_status is not 'pending'")
        elif r.expected_total is not None and r.total_value == r.expected_total and r.balance_status != BalanceStatus.balanced:
            reasons.append("total_value == expected_total but balance_status is not 'balanced'")
        elif r.expected_total is not None and r.total_value != r.expected_total and r.balance_status != BalanceStatus.not_balanced:
            reasons.append("total_value != expected_total but balance_status is not 'not_balanced'")
        if r.is_superseded and r.original_transaction_id is not None:
            reasons.append("both is_superseded=True and original_transaction_id set -- a corrected row was itself corrected")
        return "; ".join(reasons)

    samples = [
        {"transaction_id": str(r.transaction_id), "business_date": r.business_date, "reason": _reason(r)}
        for r in _cap(rows, ctx.sample_size)
    ]
    return CheckOutcome(violation_count=len(rows), sample_violations=samples)


# ---------------------------------------------------------------------------
# completed_records_without_completion_metadata
# ---------------------------------------------------------------------------

async def check_completed_records_without_completion_metadata(ctx: RunContext) -> CheckOutcome:
    eod_stmt = select(EODClosure.id, EODClosure.business_date).where(
        EODClosure.closed_automatically.is_(False), EODClosure.closed_by_user_id.is_(None)
    )
    for c in ctx.date_clauses(EODClosure.business_date):
        eod_stmt = eod_stmt.where(c)
    eod_rows = (await ctx.catalog_conn.execute(eod_stmt)).all()

    flag_stmt = select(
        DuplicateFlag.id, DuplicateFlag.transaction_id, DuplicateFlag.status
    ).where(
        DuplicateFlag.status != DuplicateFlagStatus.pending,
        or_(DuplicateFlag.reviewed_at.is_(None), DuplicateFlag.reviewed_by_user_id.is_(None)),
    )
    flag_rows = (await ctx.catalog_conn.execute(flag_stmt)).all()

    samples = [
        {
            "entity": "eod_closure",
            "id": str(r.id),
            "business_date": r.business_date,
            "reason": "manual close (closed_automatically=False) but closed_by_user_id is NULL",
        }
        for r in _cap(eod_rows, ctx.sample_size)
    ]
    remaining = max(0, ctx.sample_size - len(samples))
    samples += [
        {
            "entity": "duplicate_flag",
            "id": str(r.id),
            "transaction_id": str(r.transaction_id),
            "status": r.status.value if hasattr(r.status, "value") else r.status,
            "reason": "status is no longer 'pending' but reviewed_at/reviewed_by_user_id is NULL",
        }
        for r in _cap(flag_rows, remaining)
    ]
    return CheckOutcome(violation_count=len(eod_rows) + len(flag_rows), sample_violations=samples)


# ---------------------------------------------------------------------------
# completed_records_changed_after_completion (best-effort proxy)
# ---------------------------------------------------------------------------

async def check_completed_records_changed_after_completion(ctx: RunContext) -> CheckOutcome:
    # No updated_at/version-history column exists to answer this directly
    # (04_postgresql_and_reconciliation.md §5.4) -- this correlates AuditLog
    # timestamps against the closing EODClosure.closed_at for the same
    # business_date as an indirect proxy: "something touching a transaction
    # from this business day happened after the day closed", never "this
    # row's contents changed after completion" with certainty.
    eod_stmt = select(EODClosure.business_date, EODClosure.closed_at)
    for c in ctx.date_clauses(EODClosure.business_date):
        eod_stmt = eod_stmt.where(c)
    closures = (await ctx.catalog_conn.execute(eod_stmt)).all()

    violations = []
    for closure in closures:
        txn_stmt = select(Transaction.transaction_id).where(Transaction.business_date == closure.business_date)
        txn_ids = {str(r[0]) for r in (await ctx.catalog_conn.execute(txn_stmt)).all()}
        if not txn_ids:
            continue
        audit_stmt = select(AuditLog.action, AuditLog.timestamp, AuditLog.details).where(
            AuditLog.timestamp > closure.closed_at,
            AuditLog.action.in_(("TRANSACTION_CORRECTED", "TRANSACTION_DAY_TRANSFERRED")),
        )
        audit_rows = (await ctx.catalog_conn.execute(audit_stmt)).all()
        for a in audit_rows:
            details = a.details or ""
            for tid in txn_ids:
                if tid in details:
                    violations.append(
                        {
                            "transaction_id": tid,
                            "business_date": closure.business_date,
                            "closed_at": closure.closed_at,
                            "audit_action": a.action,
                            "audit_timestamp": a.timestamp,
                        }
                    )

    return CheckOutcome(violation_count=len(violations), sample_violations=_cap(violations, ctx.sample_size))


# ---------------------------------------------------------------------------
# header_total_vs_denominations
# ---------------------------------------------------------------------------

async def check_header_total_vs_denominations(ctx: RunContext) -> CheckOutcome:
    denom_sum = (
        select(Denomination.transaction_id, func.sum(Denomination.value).label("sum_denoms"))
        .group_by(Denomination.transaction_id)
        .subquery()
    )
    stmt = (
        select(
            Transaction.transaction_id,
            Transaction.total_value,
            denom_sum.c.sum_denoms,
            Transaction.business_date,
        )
        .select_from(
            Transaction.__table__.outerjoin(denom_sum, Transaction.transaction_id == denom_sum.c.transaction_id)
        )
        .where(Transaction.total_value != func.coalesce(denom_sum.c.sum_denoms, 0))
    )
    for c in ctx.date_clauses(Transaction.business_date):
        stmt = stmt.where(c)
    rows = (await ctx.catalog_conn.execute(stmt)).all()

    samples = []
    for r in _cap(rows, ctx.sample_size):
        sum_denoms = r.sum_denoms if r.sum_denoms is not None else type(r.total_value)(0)
        samples.append(
            {
                "transaction_id": str(r.transaction_id),
                "total_value": r.total_value,
                "sum_denominations": sum_denoms,
                "difference": r.total_value - sum_denoms,
                "business_date": r.business_date,
            }
        )
    return CheckOutcome(violation_count=len(rows), sample_violations=samples)


# ---------------------------------------------------------------------------
# batch_eod_totals_vs_transactions (recompute-and-report only, per §5.5)
# ---------------------------------------------------------------------------

async def check_batch_eod_totals_vs_transactions(ctx: RunContext) -> CheckOutcome:
    # EODClosure stores no total/count captured at closure time (§5.5), so
    # there is nothing independent to compare against and this check can
    # never detect drift -- it recomputes live sum(total_value) per
    # business_date and reports it, exactly as the source doc specifies,
    # rather than inventing a schema change to fix the underlying gap
    # (out of scope for this checker). Always "pass"; the recomputed
    # figures ride along as an additive `computed_totals_sample` field so a
    # reader still sees them without this check ever being able to "fail".
    stmt = select(EODClosure.business_date, EODClosure.status)
    for c in ctx.date_clauses(EODClosure.business_date):
        stmt = stmt.where(c)
    closures = (await ctx.catalog_conn.execute(stmt)).all()

    computed = []
    for closure in _cap(closures, ctx.sample_size):
        total_stmt = select(
            func.coalesce(func.sum(Transaction.total_value), 0), func.count(Transaction.transaction_id)
        ).where(Transaction.business_date == closure.business_date)
        total, count = (await ctx.catalog_conn.execute(total_stmt)).one()
        computed.append(
            {
                "business_date": closure.business_date,
                "status": closure.status.value if hasattr(closure.status, "value") else closure.status,
                "recomputed_total_value": total,
                "transaction_count": count,
            }
        )
    return CheckOutcome(violation_count=0, sample_violations=[], extra={"computed_totals_sample": computed})


# ---------------------------------------------------------------------------
# corrections_without_reasons
# ---------------------------------------------------------------------------

async def check_corrections_without_reasons(ctx: RunContext) -> CheckOutcome:
    # H-3/PG-5 now has a DB-level CHECK constraint
    # (ck_transactions_correction_reason_required, backend/models/
    # transaction.py) rejecting NULL/empty correction_reason on any row with
    # original_transaction_id set -- on both SQLite and PostgreSQL. This
    # check is therefore a redundant-but-cheap verification that the
    # constraint is doing its job, PLUS it catches the one gap the
    # constraint's `<> ''` comparison does not: a whitespace-only reason
    # (e.g. "   ") satisfies `IS NOT NULL AND <> ''` at the SQL level but
    # is not a real reason -- this check normalizes with TRIM() so it still
    # flags that case.
    stmt = select(
        Transaction.transaction_id, Transaction.original_transaction_id, Transaction.correction_reason, Transaction.business_date
    ).where(
        Transaction.original_transaction_id.isnot(None),
        or_(Transaction.correction_reason.is_(None), func.trim(Transaction.correction_reason) == ""),
    )
    for c in ctx.date_clauses(Transaction.business_date):
        stmt = stmt.where(c)
    rows = (await ctx.catalog_conn.execute(stmt)).all()
    samples = [
        {
            "transaction_id": str(r.transaction_id),
            "original_transaction_id": str(r.original_transaction_id),
            "correction_reason": r.correction_reason,
            "business_date": r.business_date,
        }
        for r in _cap(rows, ctx.sample_size)
    ]
    return CheckOutcome(violation_count=len(rows), sample_violations=samples)


# ---------------------------------------------------------------------------
# restricted_actions_without_approval (narrow slice, per §4.4/§5.6)
# ---------------------------------------------------------------------------

async def check_restricted_actions_without_approval(ctx: RunContext) -> CheckOutcome:
    # Narrow slice only, as the source doc is explicit about: confirms a
    # sensitive action's audit entry names a non-null actor. Does NOT verify
    # that actor held the required role/permission at the time (User.role
    # has no history table) -- that's full authorization-matrix territory,
    # intentionally Agent 3's, not duplicated here. USER_UPDATED is treated
    # as sensitive in full (not narrowed to role-changing updates only): the
    # audit `details` string ("Target user: <username>") carries no
    # structured before/after diff to detect a role change specifically, so
    # narrowing to "role-changing" updates only isn't actually implementable
    # from what's logged today -- flagged as a known limitation rather than
    # silently narrowing to something that can't be verified.
    catalog_stmt = select(AuditLog.id, AuditLog.action, AuditLog.timestamp).where(
        AuditLog.action.in_(SENSITIVE_CATALOG_ACTIONS), AuditLog.user_id.is_(None)
    )
    catalog_rows = (await ctx.catalog_conn.execute(catalog_stmt)).all()

    samples = [
        {
            "source": "audit_log",
            "id": str(r.id),
            "action": r.action,
            "timestamp": r.timestamp,
            "reason": "sensitive action with no actor (user_id IS NULL)",
        }
        for r in _cap(catalog_rows, ctx.sample_size)
    ]
    core_rows = []
    if ctx.core_conn is not None:
        core_stmt = select(CoreAuditLog.id, CoreAuditLog.action, CoreAuditLog.timestamp).where(
            CoreAuditLog.action.in_(SENSITIVE_CORE_ACTIONS), CoreAuditLog.user_id.is_(None)
        )
        core_rows = (await ctx.core_conn.execute(core_stmt)).all()
        remaining = max(0, ctx.sample_size - len(samples))
        samples += [
            {
                "source": "core_audit_log",
                "id": str(r.id),
                "action": r.action,
                "timestamp": r.timestamp,
                "reason": "sensitive action with no actor (user_id IS NULL)",
            }
            for r in _cap(core_rows, remaining)
        ]

    extra = None
    if ctx.core_conn is None:
        extra = {"note": "no --core-database-url provided -- USER_DELETED/USER_UPDATED (core_audit_log) were not checked, only catalog-DB actions"}
    return CheckOutcome(
        violation_count=len(catalog_rows) + len(core_rows), sample_violations=samples, extra=extra
    )


# ---------------------------------------------------------------------------
# records_attributed_to_missing_or_inactive_users (two-database design)
# ---------------------------------------------------------------------------

async def check_records_attributed_to_missing_or_inactive_users(ctx: RunContext) -> CheckOutcome:
    if ctx.core_conn is None:
        raise RuntimeError(
            "records_attributed_to_missing_or_inactive_users requires --core-database-url "
            "(no cross-database JOIN is possible -- PG-13)"
        )

    # Collects every distinct user_id-shaped value referenced anywhere in
    # the catalog DB, per §4.4's column list.
    columns = [
        (Transaction, "user_id"),
        (AuditLog, "user_id"),
        (DuplicateFlag, "reviewed_by_user_id"),
        (Notification, "related_user_id"),
        (Notification, "resolved_by_user_id"),
        (EODClosure, "closed_by_user_id"),
        (EODClosure, "reopened_by_user_id"),
    ]
    referenced: dict[str, list[str]] = {}
    for model, col_name in columns:
        col = getattr(model, col_name)
        stmt = select(col).where(col.isnot(None)).distinct()
        for (uid,) in (await ctx.catalog_conn.execute(stmt)).all():
            referenced.setdefault(str(uid), []).append(f"{model.__tablename__}.{col_name}")

    if not referenced:
        return CheckOutcome(violation_count=0, sample_violations=[])

    core_stmt = select(User.id, User.is_active)
    core_users = {str(uid): is_active for uid, is_active in (await ctx.core_conn.execute(core_stmt)).all()}

    missing = sorted(uid for uid in referenced if uid not in core_users)
    inactive = sorted(uid for uid in referenced if uid in core_users and not core_users[uid])

    samples = [
        {
            "user_id": uid,
            "referenced_from": sorted(set(referenced[uid])),
            "reason": "no matching row in core users table",
        }
        for uid in _cap(missing, ctx.sample_size)
    ]
    inactive_sample = [
        {"user_id": uid, "referenced_from": sorted(set(referenced[uid]))} for uid in _cap(inactive, ctx.sample_size)
    ]
    # `missing` is always a hard violation; `referenced_but_inactive` is
    # informational only (per §4.4: a user active at transaction-time and
    # deactivated later is expected, not a defect -- User has no
    # deactivated_at so we can't tell which happened) and does not count
    # toward violation_count.
    return CheckOutcome(
        violation_count=len(missing),
        sample_violations=samples,
        extra={
            "referenced_but_inactive_count": len(inactive),
            "referenced_but_inactive_sample": inactive_sample,
        },
    )


# ---------------------------------------------------------------------------
# unexpected_gaps_from_partial_processing (heuristic)
# ---------------------------------------------------------------------------

async def check_unexpected_gaps_from_partial_processing(ctx: RunContext) -> CheckOutcome:
    # (a) transactions with zero denomination children.
    zero_denom_stmt = select(Transaction.transaction_id, Transaction.business_date).where(
        Transaction.transaction_id.notin_(select(Denomination.transaction_id))
    )
    for c in ctx.date_clauses(Transaction.business_date):
        zero_denom_stmt = zero_denom_stmt.where(c)
    zero_denom_rows = (await ctx.catalog_conn.execute(zero_denom_stmt)).all()

    # (b) per-customer "suspiciously empty day": a business_date with no
    # transactions for that customer, sandwiched between two dates (day-1,
    # day+1) that do have transactions for that same customer. Bounded to
    # the calendar span actually present in the data (or the requested
    # --business-date-from/to window) -- never an unbounded scan.
    dates_stmt = select(Transaction.customer_id, Transaction.business_date).distinct()
    for c in ctx.date_clauses(Transaction.business_date):
        dates_stmt = dates_stmt.where(c)
    pairs = (await ctx.catalog_conn.execute(dates_stmt)).all()
    by_customer: dict[str, set] = {}
    for customer_id, business_date in pairs:
        by_customer.setdefault(customer_id, set()).add(business_date)

    gap_days = []
    one_day = _dt.timedelta(days=1)
    for customer_id, present in by_customer.items():
        if len(present) < 2:
            continue
        lo, hi = min(present), max(present)
        d = lo
        while d <= hi:
            if d not in present and (d - one_day) in present and (d + one_day) in present:
                gap_days.append({"customer_id": customer_id, "business_date": d})
            d += one_day

    samples = [
        {
            "kind": "transaction_with_no_denominations",
            "transaction_id": str(r.transaction_id),
            "business_date": r.business_date,
        }
        for r in _cap(zero_denom_rows, ctx.sample_size)
    ]
    remaining = max(0, ctx.sample_size - len(samples))
    samples += [{"kind": "suspiciously_empty_day", **g} for g in _cap(gap_days, remaining)]

    return CheckOutcome(violation_count=len(zero_denom_rows) + len(gap_days), sample_violations=samples)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class CheckSpec:
    check_id: str
    severity: str
    description: str
    fn: Callable[[RunContext], "object"]
    needs_core: bool = False


CHECK_CATALOG: list[CheckSpec] = [
    CheckSpec(
        "duplicate_bag_numbers",
        "high",
        "Groups transactions by case/whitespace-insensitive bag_number within "
        "(customer_id, location_id, business_date). Diagnostic-only: Wave 1 "
        "deliberately did not add a bag-uniqueness DB constraint (business "
        "decision still pending, consolidated plan §16), so a group found "
        "here is a candidate for review, not by itself proof of a bug -- it "
        "surfaces duplicates the app-level heuristic (which only compares a "
        "user's own same-day transactions against each other) can miss, "
        "e.g. the same bag entered by two different cashiers.",
        check_duplicate_bag_numbers,
    ),
    CheckSpec(
        "duplicate_idempotency_keys",
        "high",
        "idempotency_keys.key is a DB-level unique primary key, so a literal "
        "SQL-level duplicate key is structurally impossible -- this instead "
        "verifies the idempotency logic's semantic correctness: no key "
        "references a nonexistent transaction_id, and no two different keys "
        "resolve to the same transaction_id within the same scope (which "
        "would indicate the idempotency check-and-insert itself is broken, "
        "not a hardware/DB defect).",
        check_duplicate_idempotency_keys,
    ),
    CheckSpec(
        "orphan_denominations",
        "medium",
        "Denomination rows with no matching Transaction row. Should be "
        "structurally impossible given the FK, but SQLite FK enforcement is "
        "unconfirmed (PG-12) -- also useful as a live PostgreSQL FK-health "
        "signal.",
        check_orphan_denominations,
    ),
    CheckSpec(
        "missing_audit_events",
        "high",
        "Every is_superseded=True transaction must have a matching "
        "TRANSACTION_CORRECTED AuditLog entry; every EODClosure must have a "
        "matching EOD_CLOSED AuditLog entry. (The source doc's third "
        "example, a USER_CREATED entry for every core User, is omitted: "
        "confirmed that auth_service.create_user never audits user "
        "creation at all in this codebase, so that sub-check would flag "
        "every user as a false positive.)",
        check_missing_audit_events,
    ),
    CheckSpec(
        "invalid_state_transitions",
        "high",
        "Rows with duplicate_flag_status outside its known vocabulary; "
        "balance_status/expected_total combinations calculate_balance_status() "
        "could never produce; a row with both is_superseded=True and "
        "original_transaction_id set simultaneously.",
        check_invalid_state_transitions,
    ),
    CheckSpec(
        "completed_records_without_completion_metadata",
        "medium",
        "EODClosure with closed_automatically=False and closed_by_user_id "
        "IS NULL (a manual close must have an actor); DuplicateFlag.status "
        "!= pending with reviewed_at/reviewed_by_user_id IS NULL.",
        check_completed_records_without_completion_metadata,
    ),
    CheckSpec(
        "completed_records_changed_after_completion",
        "medium",
        "Best-effort proxy only (no updated_at/version-history column "
        "exists on any table to answer this directly): correlates AuditLog "
        "entries referencing a transaction against that transaction's "
        "EODClosure.closed_at for the same business_date. Can say activity "
        "happened after the day closed, never that a specific row's "
        "contents changed.",
        check_completed_records_changed_after_completion,
    ),
    CheckSpec(
        "header_total_vs_denominations",
        "high",
        "Transaction.total_value must equal sum(Denomination.value) for its "
        "child rows. Both Numeric(12,2), so any nonzero difference is a "
        "real defect, not a rounding artifact.",
        check_header_total_vs_denominations,
    ),
    CheckSpec(
        "batch_eod_totals_vs_transactions",
        "medium",
        "EODClosure stores no total/count captured at closure time, so this "
        "can only recompute sum(Transaction.total_value) per business_date "
        "live and report it -- it cannot detect drift against an "
        "independently-captured snapshot, and always passes; the recomputed "
        "figures ride along in computed_totals_sample.",
        check_batch_eod_totals_vs_transactions,
    ),
    CheckSpec(
        "corrections_without_reasons",
        "high",
        "Every row with original_transaction_id set must have a non-empty, "
        "non-whitespace correction_reason (see finding PG-5). Now backstopped "
        "by ck_transactions_correction_reason_required (Wave 1) on both "
        "dialects -- this check is a redundant-but-cheap verification that "
        "the constraint is doing its job, plus it catches a whitespace-only "
        "reason, which satisfies the constraint's `<> ''` but isn't a real "
        "reason.",
        check_corrections_without_reasons,
    ),
    CheckSpec(
        "restricted_actions_without_approval",
        "medium",
        "Narrow slice only: verifies sensitive AuditLog/CoreAuditLog action "
        "types (TRANSACTION_DAY_TRANSFERRED, EOD_REOPENED, USER_DELETED, "
        "USER_UPDATED) always carry a non-null actor user_id. Does NOT "
        "verify the actor held the required role at the time -- that needs "
        "point-in-time role history, which doesn't exist. Not a duplicate "
        "of Agent 3's authorization-matrix work.",
        check_restricted_actions_without_approval,
    ),
    CheckSpec(
        "records_attributed_to_missing_or_inactive_users",
        "high",
        "Every user_id-shaped column value referenced in the catalog DB "
        "must resolve to a real, existing core user (Python-side set "
        "difference against --core-database-url; no cross-database JOIN is "
        "possible -- PG-13). Reports 'missing' (hard violation) and "
        "'referenced_but_inactive' (informational only) separately.",
        check_records_attributed_to_missing_or_inactive_users,
        needs_core=True,
    ),
    CheckSpec(
        "unexpected_gaps_from_partial_processing",
        "medium",
        "Heuristic only: (a) Transaction rows with zero Denomination "
        "children; (b) a business_date with no transactions for a customer "
        "sandwiched between two dates that do have transactions for that "
        "same customer ('suspiciously empty day').",
        check_unexpected_gaps_from_partial_processing,
    ),
]

CHECK_BY_ID: dict[str, CheckSpec] = {c.check_id: c for c in CHECK_CATALOG}

NOT_APPLICABLE: dict[str, str] = {
    "duplicate_transaction_references": (
        "Transaction has no reference/business-number field of any kind; "
        "transaction_id (UUID4, PK) is the only identifier and comparing "
        "UUIDs for 'duplicates' is meaningless by construction. Still "
        "blocked on a reference-field design -- Wave 1 did not add one."
    ),
    "receipt_data_vs_stored_values": (
        "No PDF/receipt/label subsystem exists anywhere in this repository."
    ),
}

ALL_CHECK_IDS = list(CHECK_BY_ID.keys()) + list(NOT_APPLICABLE.keys())
