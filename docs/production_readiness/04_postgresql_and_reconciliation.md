# 04 — PostgreSQL, Data Integrity, and Reconciliation (Agent 4)

Date: 2026-08-22
Scope: Read-only, documentation/design only. No schema changes, no migrations
written, no PostgreSQL server installed or started. Every finding below is
tagged **Confirmed** (verified by reading source, running `grep`, or
empirically testing SQLAlchemy behavior in the pre-built baseline venv
against `postgresql+asyncpg://` / `sqlite+aiosqlite://` URLs with no live
server — engine/DDL/type-compilation introspection only, no network I/O) or
**Assumed** (reasoned from documented SQLAlchemy/PostgreSQL behavior, not
directly exercised in this sandbox — flagged for Phase 5 execution against a
real PostgreSQL instance).

Terminology: "core DB" = `core.db` (users, `core_audit_log`); "catalog DB" =
one of 4 physically separate databases (VMS/Dayshift/Complete/ESNF), each
holding its own `transactions`, `denominations`, `customers`, `locations`,
`audit_log`, `notifications`, `duplicate_flags`, `eod_closures`.

---

## 1. PostgreSQL-readiness findings

### Critical

**PG-1. No connection-pool tuning for PostgreSQL, across 5 concurrent engines per process — Confirmed.**
`backend/core/database.py:22-49` creates one `create_async_engine(url, echo=settings.debug, connect_args=_connect_args(url))` per database (1 core + 4 catalog = 5 engines total) with no `pool_size`, `max_overflow`, `pool_timeout`, or `pool_recycle` arguments anywhere (confirmed: `grep -rn "pool_size\|max_overflow\|pool_recycle\|pool_timeout\|pool_pre_ping"` across `backend/` returns zero hits). `_connect_args` (`database.py:10-11`) is confirmed to be the **only** dialect-conditional code in the codebase (`grep -rn "dialect\|postgres\|sqlite" backend/` shows every other hit is either a comment, a URL string, or `_connect_args`/`backup_service.py` itself — no other branch exists).

Empirically confirmed in the baseline venv (SQLAlchemy 2.0.35):
```
create_async_engine("sqlite+aiosqlite:///./x.db")            -> NullPool
create_async_engine("postgresql+asyncpg://u:p@localhost/db") -> AsyncAdaptedQueuePool,
    pool_size=5, max_overflow=10, pool_timeout=30.0s, pool_recycle=-1 (never)
```
On SQLite, `NullPool` means this gap is completely invisible today — every checkout opens a fresh file handle and closes it, so there is nothing to exhaust. The instant any `DATABASE_URL_*` points at Postgres, all 5 engines silently pick up `AsyncAdaptedQueuePool` defaults. With **N** app worker processes (`run_backend.py:8-14` confirms today's dev entrypoint runs a single `uvicorn` process with no worker count set — Confirmed), each process independently instantiates its own 5 engines at import time, so total possible open connections = `N × 5 × (5 + 10)` = up to **75 connections from a single worker**, `150` from two, etc. — against Postgres's common default `max_connections=100`, this exhausts the server with a modest worker count, before counting Alembic runs, the integrity checker (§3), `psql`, or monitoring tools that also need a slot. `pool_recycle=-1` additionally means idle connections are never proactively closed, so any load balancer, firewall, or managed-Postgres idle-connection timeout sitting in front of the database (common on RDS/Cloud SQL/PgBouncer) will silently kill connections the pool still believes are alive, surfacing as intermittent `ConnectionDoesNotExistError`/`OperationalError` at checkout time.

*Recommended defaults for this workload* (internal cash-processing app, dozens of concurrent cashier/supervisor sessions per catalog, not internet-scale traffic; core DB touched only for auth/user admin, catalog DBs carry the transaction/EOD load):
| Engine | pool_size | max_overflow | pool_timeout | pool_recycle | pool_pre_ping |
|---|---|---|---|---|---|
| core | 5 | 5 | 10s | 1800s (30 min) | True |
| each catalog (×4) | 10 | 10 | 10s | 1800s (30 min) | True |

`pool_timeout=10s` (down from the 30s default) so a saturated pool fails fast into a request-level error instead of hanging; `pool_pre_ping=True` is not set anywhere today (confirmed) and is cheap insurance against the stale-connection problem above. These are starting points, not final numbers — validate them against the real Postgres `max_connections`, real worker count, and real concurrent-user count in Phase 3/5. Given the 5-engines-×-N-workers multiplication, seriously evaluate **PgBouncer in transaction-pooling mode** in front of Postgres before going multi-worker — note `asyncpg` + PgBouncer transaction mode has known caveats (no session-level prepared-statement caching, no advisory locks across transactions) that should be validated in Phase 3, not assumed here.

**PG-2. Native-enum dialect divergence: SQLite silently allows invalid enum values; PostgreSQL enforces them via a real `CREATE TYPE ... AS ENUM` — Confirmed empirically.**
Affected columns: `Transaction.balance_status`, `User.role`, `Notification.severity`, `Notification.status`, `DuplicateFlag.status`, `EODClosure.status` — every `SAEnum(...)` column across `backend/models/*.py`. Verified by compiling `CreateTable` DDL for an `Enum` column under both dialects in the baseline venv:
```
postgresql: CREATE TABLE x (balance_status balancestatus NOT NULL)   -- real ENUM type, rejects unlisted values
sqlite:     CREATE TABLE x (balance_status VARCHAR(12) NOT NULL)      -- plain VARCHAR, NO CHECK constraint
```
SQLAlchemy's SQLite `Enum` compiler only emits a `CHECK` constraint when `create_constraint=True` is explicitly passed to `SAEnum(...)` — confirmed nowhere in `backend/models/*.py` is this passed, so on SQLite (today's only dev/test/CI database) these columns have **zero DB-level protection** against invalid values; any bug that writes a stray string is silently accepted and the automated test suite (127 passing tests, all SQLite) can never surface it. Point a `DATABASE_URL_*` at Postgres and the exact same code, on its very first write of a bad value, starts raising a hard `InvalidTextRepresentationError` that has never been exercised in CI.

Separately confirmed: SQLAlchemy's `Enum` type binds by the Python **enum member name**, not `.value` — e.g. `BalanceStatus.balanced` (value `"BALANCED"`) is stored/compared as the string `"balanced"`. This matches what every migration already encodes (`alembic_catalog/versions/7b3f53f27b9a_initial.py:56`: `sa.Enum('balanced', 'not_balanced', 'pending', name='balancestatus')`), so **models and migrations agree on the wire format today** — this is not itself a drift bug, but it is a sharp edge for any future non-ORM writer (a raw-SQL fix script, an ETL job, a DBA console session) that assumes the enum's declared *value* (`"BALANCED"`) is what's stored, when it's actually the *name* (`"balanced"`).

Forward-looking risk: adding or renaming an enum member later requires an explicit `ALTER TYPE ... ADD VALUE` migration on Postgres (which cannot run inside the same transaction as other DDL on Postgres <12, and which Alembic's autogenerate does not produce correctly out of the box — it will instead try to `DROP TYPE`/`CREATE TYPE` and fail against any table still using the old type). No such migration exists yet in `alembic_catalog/versions/` because the team has only ever run against SQLite, where this problem doesn't exist. Also note: `op.drop_column` on an enum-typed column (e.g. any `downgrade()` in `alembic_catalog/versions/275f950e5d3d_notifications_and_duplicate_flags.py`) does not drop the underlying Postgres `TYPE` — it's orphaned, and a subsequent `upgrade()` re-running `CREATE TYPE` of the same name will fail. This only matters if downgrades are ever actually run against Postgres, but it's an untested path.

**PG-3. No row-level locking or explicit isolation control anywhere; multiple check-then-act races are exposed under PostgreSQL's default READ COMMITTED concurrency — Confirmed (absence) + reasoned (exposure).**
Confirmed via `grep -rn "for update\|with_for_update\|FOR UPDATE\|isolation_level\|SERIALIZABLE" backend/` → zero hits anywhere in the codebase. No code path sets an explicit isolation level, so both SQLite and (once adopted) `asyncpg`/PostgreSQL run at their respective drivers' default — for `asyncpg`, that's PostgreSQL's server default, READ COMMITTED. Concretely exposed races (DB-mechanics view — cross-references Agent 1's concurrency findings at the application layer):
- `EODRepository.close()` (`backend/repositories/eod_repository.py:19-42`) and its caller `EODService.close_day()` (`backend/services/eod_service.py:23-32`): `is_day_closed()` read, then a separate insert-or-update, no `SELECT ... FOR UPDATE`. `EODClosure.business_date` does carry a `unique=True` index (`backend/models/eod.py:19`), so two concurrent **first-time** closes for the same business date can't both insert — but the loser gets a raw, unhandled `IntegrityError` (neither `eod_repository.py` nor `eod_service.py` catches it) instead of the intended `ValueError("already closed")`, surfacing as a 500 rather than the graceful conflict message the code clearly intends. The concrete production trigger is real and already wired up: `backend/services/eod_scheduler.py:17-33` runs an automatic midnight close for every catalog, which can race a supervisor's manual close through the EOD page at the same instant.
- `TransactionService.create_transaction()` (`backend/services/transaction_service.py:49-93`) checks `eod_service.is_day_closed(today)` and then creates the transaction with no lock held across the gap — a transaction can land in the small window between the check and a concurrent EOD close for the same business date.
- `DuplicateDetectionService.check_for_duplicate()` (`backend/services/duplicate_detection_service.py:37-73`) reads existing same-user/same-business-date candidates, then decides whether to flag — no lock, and (see PG-4) no unique constraint exists to catch what the read misses. Two identical bag-number submissions within milliseconds of each other both pass the "no candidate yet" read and both insert; the duplicate flag is created only after the fact, non-atomically. This is the same defect `00_baseline.md`'s mapping table already flagged at the application layer (row 63) — PG-3/PG-4 are its DB-mechanics root cause and remedy.

*Recommended DB-mechanics remedies (design only, not implemented in Phase 1):* (a) `SELECT ... FOR UPDATE` on the `EODClosure` row (or the absence-of-row case) inside `EODRepository.close()`/`is_day_closed()` when called from a code path that intends to act on the result; (b) catch `IntegrityError` around the `EODClosure` insert and translate it to the existing `ValueError("already closed")` message — a one-line, low-risk fix once Phase 2 implementation begins; (c) a real **unique constraint** to backstop duplicate-bag detection at the DB level (see PG-4) rather than relying solely on an app-level read-then-write heuristic; (d) for `create_transaction` vs EOD close, either take a `SELECT ... FOR UPDATE` on the day's `EODClosure` row before creating the transaction, or accept the current race as low-probability/low-impact and instead detect it after the fact via the integrity checker's "transactions dated into a closed day" check (§3).

### High

**PG-4. `bag_number` has no unique constraint; duplicate-bag prevention is a post-hoc application heuristic, not a DB invariant — Confirmed.**
`backend/models/transaction.py:29`: `bag_number: Mapped[str] = mapped_column(String(100), nullable=False)` — no `unique=True`, no `UniqueConstraint`. Confirmed via `grep -rn "unique=True\|UniqueConstraint" backend/models/` — the only two unique constraints in the entire schema are `EODClosure.business_date` (`eod.py:19`) and `User.username` (`user.py:21`). Nothing stops two transactions with the identical `bag_number` (same or different case/whitespace) from both being persisted; `DuplicateDetectionService` (see PG-3) only flags the second one *after* both rows already exist. Whether a DB-level unique constraint is even the right fix depends on the true business uniqueness scope (globally unique bag number? unique per customer+location+business_date? bags can legitimately be reused across days?) — that scoping decision belongs to Agent 1/the business, not this document; flagged here as the concrete DB-mechanics gap once that scope is decided.

**PG-5. `correction_reason` is nullable at the DB level with no CHECK constraint tying it to the correction workflow it exists for — Confirmed.**
`backend/models/transaction.py:70`: `correction_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)`, and no migration (`alembic_catalog/versions/e3a9c6d1f480_transaction_correction.py`, which introduced the column, or any later one) adds a `CHECK` constraint. Enforcement today is entirely at the Pydantic/API boundary: `backend/schemas/transaction.py:79` — `reason: str = Field(..., min_length=1)` — required only for requests that go through `TransactionService.correct_transaction()` (`transaction_service.py:197-257`, which does pass `reason` through as `correction_reason` at `line 245`). Any write path that bypasses that one call — a future bulk-correction endpoint, an admin data-fix script, a direct DB statement — can leave a row with `original_transaction_id` set and `correction_reason` NULL, silently defeating the append-only workflow's audit intent. Recommended for Phase 2 design: `CHECK (original_transaction_id IS NULL OR (correction_reason IS NOT NULL AND correction_reason <> ''))`.

**PG-6. No backup mechanism works for PostgreSQL at all, and no restore path exists for *any* database — Confirmed by full read of `backend/services/backup_service.py`.**
The entire module is SQLite-specific: it `import sqlite3` directly (`backup_service.py:9`), resolves each database's file path by string-splitting the URL on `"///"` (`_sqlite_path`, `backup_service.py:20-25` — this assumes a `sqlite+aiosqlite:///<path>` shape; run against a `postgresql+asyncpg://user:pass@host:5432/dbname` URL, `url.split("///", 1)[1]` raises `IndexError` since there's no third `/` — it does not silently produce garbage, it crashes), and performs the copy via `sqlite3.Connection.backup()` (`_backup_one_sync`, `backup_service.py:35-45`), which has no PostgreSQL equivalent in the standard library. **There is no restore function anywhere in the codebase** — confirmed via `grep -rn "restore" backend/ -i` = zero hits outside this document. Only `create_backup()` and `list_backups()` exist (`backup_service.py:48-83`); no code path has ever proven a captured backup is actually restorable, manually or automatically. For a cash-handling system of record this is a significant operational gap independent of the Postgres question, and it becomes a hard blocker the moment Postgres is adopted: the module needs a complete rewrite around `pg_dump`/`pg_restore` (or `pg_basebackup`/WAL archiving for point-in-time recovery), run as external subprocesses rather than an in-process Python DB-API call, plus a genuine restore-and-validate procedure (restore to a scratch database, run the integrity checker from §3 against it, only then consider the backup good) that doesn't exist for SQLite either today.

**PG-7. One raw-SQL migration statement has dialect-dependent semantics that have never been verified against real PostgreSQL — Confirmed (statement) + Assumed (behavioral divergence, pending Phase 5).**
`alembic_catalog/versions/b17cb77d7715_eod_and_business_date.py:42`: `op.execute("UPDATE transactions SET business_date = date(created_at) WHERE business_date IS NULL")`. `created_at` is `DateTime(timezone=True)` (`transaction.py:38-40`). On SQLite, `created_at` is stored as an ISO-8601 string and `date()` extracts the calendar-date substring with no timezone conversion. On PostgreSQL, `created_at` becomes a real `timestamptz` (stored internally as UTC), and PostgreSQL's function-style type cast `date(timestamptz_expr)` converts to the **session's `timezone` GUC** before truncating — so for rows created in the hours around local midnight, the same underlying instant can produce a *different* calendar date than SQLite's naive substring extraction, depending on what timezone the migration is run under. Practical exposure today is low: this statement only ever runs against an **empty** `transactions` table, because `init_databases()` (`backend/core/database.py:92-105`) uses `metadata.create_all()` + `alembic stamp head` for every fresh install rather than replaying migration history (also flagged in `00_baseline.md`), so the only way this `UPDATE` ever touches real rows is a deliberate `alembic upgrade head` run against a populated database — which has never happened for this codebase (SQLite has been the only database in production so far) and would only become relevant if someone attempts a genuine SQLite→PostgreSQL data migration via dump/restore + replaying Alembic history rather than a logical ETL. Flagged as a confirmed landmine, not a confirmed active bug — **requires Phase 5 verification** against a real PostgreSQL instance with a non-UTC session timezone to confirm the divergence and its magnitude.

### Medium

**PG-8. UUID primary keys are generated client-side only (`default=uuid.uuid4`), never `server_default=` — Confirmed, 8 occurrences.**
`grep -rn "default=uuid.uuid4" backend/models/` hits every PK across `core_audit.py:11`, `duplicate.py:18`, `transaction.py:21,82,96`, `eod.py:18`, `notification.py:33`, `user.py:19` — 100% Python-side generation, never `server_default=text("gen_random_uuid()")` or similar. Because UUID4 draws from 122 bits of randomness, collision probability is cryptographically negligible regardless of how many app-server processes generate IDs concurrently against Postgres — **this is not a correctness bug**. It is, however, an operational-hardening gap worth deciding explicitly before a Postgres cutover: DB-side generation (`server_default=text("gen_random_uuid()")`, built into PostgreSQL 13+ core, needs the `pgcrypto` extension on 12 and earlier) makes IDs available to any non-ORM writer (a raw-SQL fix script, a future service writing directly to the DB) and removes one more thing the Python layer has to get right. Recommend adopting `server_default` for new tables going forward and leaving existing tables as-is (a full backfill/constraint-swap for existing PKs is unnecessary churn for zero correctness benefit).

**PG-9. No FK `ON DELETE` behavior specified anywhere — Confirmed.**
`grep` of every `ForeignKeyConstraint(...)`/`ForeignKey(...)` call across all migrations and models shows no `ondelete=` argument anywhere — every FK defaults to PostgreSQL's implicit `NO ACTION` (and to SQLite's default, which is a no-op unless `PRAGMA foreign_keys=ON` is set — see PG-12, not confirmed either way here). Concretely: `Denomination.transaction_id → transactions.transaction_id` (`transaction.py:83-85`) relies entirely on the ORM's `cascade="all, delete-orphan"` (`transaction.py:74-76`) for cleanup, and that cascade **only fires through the SQLAlchemy ORM session** — a raw `DELETE FROM transactions WHERE ...` (a DBA console session, a future bulk-cleanup script) leaves orphaned `Denomination` rows behind with no DB-level protection. `Transaction.original_transaction_id` (self-referential, `transaction.py:66-68`) and both FKs on `DuplicateFlag` (`duplicate.py:19-24`) have the same gap. Given this is an append-only, audit-oriented schema where rows are essentially never expected to be hard-deleted in normal operation, the likely-correct fix is `ondelete="RESTRICT"` (fail loudly on an unexpected delete) rather than `CASCADE` — but that's a Phase 2 design decision, flagged here as a gap, not prescribed.

**PG-10. `business_date` (naive local date) vs `created_at` (UTC, tz-aware) depends entirely on the app server's OS timezone matching the business's operating timezone, with no explicit timezone configuration anywhere — Confirmed (mechanism) + reasoned (impact).**
`transaction.py:46-48`: `business_date: Mapped[date] = mapped_column(Date, default=lambda: datetime.now().date(), ...)` — naive, OS-local. Same pattern at the call site: `transaction_service.py:50`, `today = datetime.now().date()`. `backend/core/config.py` has no `TZ`/timezone setting at all (confirmed by full read) — the business's operating timezone is never asserted anywhere in code or config, only implicitly assumed to equal the app server's OS timezone. If the server's OS timezone is UTC (a common container/cloud default) while the business operates in, say, CET/CEST, every transaction entered in the 1-2 hour window after local midnight is stamped with the wrong `business_date` — a full day early relative to the operator's actual business day — and this stays internally self-consistent (the scheduler's `auto_close_all_catalogs`, `eod_scheduler.py:18`, uses the identical `datetime.now().date() - timedelta(days=1)` convention) but wrong relative to reality. DST transitions (the repeated hour on fall-back, the skipped hour on spring-forward) have no special handling anywhere in the codebase — `datetime.now()` on a naive-local OS clock just does whatever the OS does, uninspected.

**PG-11. Two independently-tracked "duplicate status" fields, different vocabularies, no DB-level constraint tying them together — Confirmed.**
`Transaction.duplicate_flag_status` (`transaction.py:49-53`, free-text `String(30)`, vocabulary `none`/`pending_review`/`confirmed_duplicate`/`dismissed`, explicitly denormalized per its own comment) vs `DuplicateFlag.status` (`duplicate.py:32-34`, real `SAEnum(DuplicateFlagStatus)`, vocabulary `pending`/`confirmed`/`dismissed`) — related concepts, different string vocabularies, kept in sync only by application code (`duplicate_detection_service.py:52-53,88-90`, `TransactionRepository.set_duplicate_flag_status`). No DB constraint or trigger enforces agreement. Not itself a Postgres-portability issue, but directly relevant to the integrity checker design (§3): it must cross-validate these two fields independently rather than treat either as authoritative.

### Low

**PG-12. `PRAGMA foreign_keys` state is unconfirmed for the SQLite dev/test databases — Confirmed absence of any setting, actual runtime state not verified.**
`grep -rn "foreign_keys" backend/` (case-sensitive and via the broader dialect grep) returns no hits — nothing in `_connect_args` (`database.py:10-11`) or anywhere else enables `PRAGMA foreign_keys=ON`, and aiosqlite/SQLAlchemy do not turn it on by default. If FK enforcement is in fact off on SQLite today, every FK in the dev/test schema is silently unenforced — meaning dev/test could be masking FK-violating writes that PostgreSQL (which always enforces FKs) would reject on day one of a cutover. **Needs Phase 5 confirmation** by querying `PRAGMA foreign_keys` against a running dev DB (not done here — no live DB in this sandbox).

**PG-13. Cross-database `user_id` references are unenforceable by any DB, on SQLite or PostgreSQL, by architectural design — Confirmed correct and unavoidable, documented here as a structural constraint on the integrity checker, not a defect.**
`user_id` columns across `Transaction`, `AuditLog`, `DuplicateFlag.reviewed_by_user_id`, `Notification.related_user_id`/`resolved_by_user_id`, `EODClosure.closed_by_user_id`/`reopened_by_user_id` are all plain UUID columns, explicitly and correctly commented as "not a ForeignKey: users live in the core database" in every model file. This is unavoidable given the architecture (4 physically separate catalog databases + 1 core database) and would remain unavoidable after a PostgreSQL cutover unless the catalogs and core were consolidated into one physical database with cross-schema FKs (a re-platforming decision out of scope for this document, worth surfacing to the architecture owner separately). Consequence: no DB-level mechanism can ever detect or prevent a catalog-DB row pointing at a deleted or nonexistent user — this has to be checked out-of-band, which is exactly what the checker design in §3 does.

**PG-14. Engine lifecycle disposal is handled correctly, and the team has already anticipated the Postgres pooling implication — Confirmed **positive** finding, no action needed.**
`backend/main.py:55-62` disposes `core_engine` and every catalog engine during `lifespan()` shutdown, with an explicit comment: *"harmless for local SQLite files but a real leak pattern were this ever pointed at Postgres"* — the team has already reasoned about this correctly. `tests/conftest.py:46-57` independently disposes the same engines for the same reason (the app's own lifespan is bypassed by the ASGI test transport). Recorded so a later implementer doesn't waste time re-deriving what's already right.

---

## 2. Migration-vs-model constraint agreement (Task #2)

Full read of both Alembic histories against the current model classes, field by field:

- **Core DB** (`alembic/versions/cc53366001be_initial.py`, 1 migration) vs `backend/models/user.py` + `core_audit.py`: **exact agreement**, no drift. Every column, type, nullability, and the two indexes (`ix_users_username` unique, `ix_core_audit_log_timestamp`) match today's models precisely.
- **Catalog DB** (7 migrations, `7b3f53f27b9a` → `f7d0b2e5a913`) vs `Transaction`, `Denomination`, `AuditLog`, `Customer`, `Location`, `EODClosure`, `Notification`, `DuplicateFlag`: **exact agreement**, no drift, traced column-by-column:
  - `Transaction`'s full 17-column shape is the union of the initial migration (10 columns) plus `business_date` (`b17cb77d7715`), `duplicate_flag_status` (`275f950e5d3d`), `was_transferred` (`18ca1adb2403`), `wallet_id` (`a1c7e4f9d2b6`), and `original_transaction_id`/`is_superseded`/`correction_reason` (`e3a9c6d1f480`) — every one of those columns' nullability and defaults in the migrations matches the model exactly, including the two SQLite-specific `server_default=sa.false()` boolean backfills and the `business_date` nullable-then-tightened two-step (`b17cb77d7715:41-45`) needed because SQLite can't add a `NOT NULL` column with no default to a populated table.
  - `EODClosure.closed_by_user_id` is `NOT NULL` in the table's own creation (`b17cb77d7715:28`) and correctly relaxed to nullable in `f7d0b2e5a913:31-32` to support the automatic-close scheduler — matches `Optional[uuid.UUID]` in today's model exactly.
  - No orphaned/unused columns, no model field without a corresponding migration, no migration adding a column the model no longer declares.
- The one place "agreement" is nominal rather than semantic is the enum-column dialect gap already covered in **PG-2**: migrations and models agree on *which* string values are legal, but only PostgreSQL's native `ENUM` type actually **enforces** that agreement at the DB level — SQLite accepts anything.
- Minor, harmless redundancy (not a finding, noted for completeness): `alembic_catalog/versions/7b3f53f27b9a_initial.py:66` creates a non-unique index `ix_transactions_transaction_id` on a column that is already the table's primary key (and therefore already indexed by both SQLite and PostgreSQL automatically) — negligible extra write overhead, not worth a migration to remove.

**Verdict:** the migration history is unusually clean relative to the models — this codebase does not have the "model drift outpaced migrations" problem the brief asked to check for. The real gap is the one covered in §1: migrations correctly describe *SQLite's* schema evolution but have never been run against, or verified for, PostgreSQL's stricter semantics (PG-2, PG-7).

---

## 3. Required PostgreSQL checks — to be executed in Phase 5, not now

These require a real PostgreSQL instance and are **not run in Phase 1**. Listed here so Phase 5 has a concrete checklist; each maps to a finding above where one exists.

| # | Check | Confirms/refutes | Depends on |
|---|---|---|---|
| 1 | `alembic upgrade head` from empty, on both `alembic.ini` and `alembic_catalog.ini` (all 4 catalog codes via `-x catalog=<code>`), against a real Postgres DB — does it complete without manual intervention? | PG-2 (enum `CREATE TYPE` ordering), PG-7 (raw-SQL `date()` statement) | PostgreSQL server, `asyncpg` installed |
| 2 | Insert an invalid enum string directly (bypassing the ORM) into `balance_status`/`role`/etc. — confirm Postgres rejects it where SQLite silently accepted it | PG-2 | Check 1 |
| 3 | `alembic downgrade`/`upgrade` round-trip on the catalog history — confirm no orphaned `CREATE TYPE` failure on re-upgrade | PG-2 | Check 1 |
| 4 | Run `init_databases()`'s `create_all()` + `stamp head` path (today's actual fresh-install flow) against Postgres and diff the resulting schema against a real `alembic upgrade head` from empty — do they produce identical DDL? | Whether the "stamp instead of run" shortcut (`database.py:92-105`) is safe on Postgres, not just SQLite | Check 1 |
| 5 | Concurrent `EOD close` load test (N parallel requests for one new `business_date`) against Postgres with `pool_size` per PG-1's recommended defaults — confirm whether the unhandled `IntegrityError` in PG-3 actually surfaces, and under what request volume the pool saturates | PG-1, PG-3 | Checks 1, load-test harness (Agent 2) |
| 6 | Confirm `PRAGMA foreign_keys` state on the current SQLite dev DB, then confirm equivalent FK-violating writes are rejected by Postgres | PG-12 | Running dev SQLite DB |
| 7 | Measure actual connection count under realistic worker/traffic levels (Agent 2's load-test numbers) against the PG-1 recommended pool defaults; tune `pool_size`/`max_overflow` from real numbers, not the estimate here | PG-1 | Agent 2's load-test results |
| 8 | `pg_dump`/`pg_restore` full-cycle test: dump, restore to a scratch DB, run the integrity checker (§4) against the restored copy, confirm zero new violations introduced by the dump/restore round-trip itself | PG-6 | PostgreSQL server, checker implementation (Phase 2+) |
| 9 | Session-timezone experiment: run `alembic upgrade head` from empty with the Postgres session `timezone` GUC set to a non-UTC zone, seed a few `created_at` values near local midnight, confirm whether PG-7's predicted divergence is real and its magnitude | PG-7 | Check 1 |

---

## 4. Independent integrity checker — command/output contract proposal (design only)

Design only — **no checker code is written in Phase 1.** Proposed as a standalone script under `tools/integrity_check/`, invoked via `python -m tools.integrity_check`, so it can run against a live DB, a restored backup (feeding PG-6's restore-and-validate gap), or a Phase 5 Postgres instance without importing/booting the FastAPI app. It never writes — every check is a read-only `SELECT`.

### 4.1 CLI contract

```
python -m tools.integrity_check \
    --catalog {vms|dayshift|complete|esnf|all} \
    --catalog-database-url <URL>            # required unless --catalog all with env fallback
    --core-database-url <URL>               # required for the cross-DB user checks (PG-13)
    [--business-date-from YYYY-MM-DD] [--business-date-to YYYY-MM-DD]
    [--checks all|<comma-separated check ids>]   # default: all implemented checks
    [--severity-threshold critical|high|medium|low]  # only these severities affect exit code; default: low (everything counts)
    [--sample-size N]                       # violations included per check in output, default 20
    [--format json|text]                    # default: text for a terminal, json implied by --output-file
    [--output-file PATH]                    # default: stdout
    [--redact-connection-strings / --no-redact-connection-strings]  # default: redact (strip credentials before they ever reach output)
```

- `--catalog all` loops internally over the 4 catalog codes, running the same core-DB comparison once per catalog (user existence/active-state doesn't change per catalog, but the *referencing* rows do), and emits one JSON object **per catalog** plus a top-level aggregate wrapper — never silently merges catalogs, since a violation must always be traceable to exactly one physical database.
- Database URLs are passed explicitly on the command line (or read from the same `DATABASE_URL_*` env vars the app itself uses, as a convenience default via `--catalog vms` alone with no `--catalog-database-url`) rather than importing `backend.core.config.settings`, so the tool can be pointed at a restored backup or a different environment entirely without needing the app's own `.env`.
- Read-only by construction: opens each session, runs `SELECT`-only queries, never calls `.flush()`/`.commit()` on anything but a read-only transaction. Recommend actually opening the DB connection with `isolation_level="AUTOCOMMIT"` + explicit read-only `SET TRANSACTION READ ONLY` on Postgres as defense-in-depth against a future check accidentally mutating data.

### 4.2 Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks at or above `--severity-threshold` passed (checks marked `not_applicable` don't count against this) |
| 1 | At least one check at or above `--severity-threshold` found a violation |
| 2 | Tool/connection error — couldn't connect, couldn't run a query, bad arguments (never "found a data problem"; distinguished so CI can tell "the checker broke" apart from "the checker found something") |

### 4.3 JSON output schema

```json
{
  "tool_version": "0.1.0",
  "run_id": "b1f2c3d4-...",
  "started_at": "2026-08-22T10:15:00Z",
  "finished_at": "2026-08-22T10:15:04Z",
  "catalog": "vms",
  "catalog_database_url_redacted": "postgresql+asyncpg://***@db-host:5432/catalog_vms",
  "core_database_url_redacted": "postgresql+asyncpg://***@db-host:5432/core",
  "scope": {
    "business_date_from": "2026-08-01",
    "business_date_to": "2026-08-22",
    "rows_scanned": {"transactions": 4213, "denominations": 18904, "eod_closures": 22, "duplicate_flags": 6, "notifications": 41}
  },
  "checks_run": ["duplicate_bag_numbers", "orphan_denominations", "header_total_vs_denominations", "..."],
  "checks_not_applicable": [
    {"check_id": "duplicate_transaction_references", "reason": "no reference field exists on Transaction; blocked on Agent 1's reference-field design"},
    {"check_id": "duplicate_idempotency_keys", "reason": "no idempotency-key column/table exists anywhere in the schema; blocked on Agent 1's idempotency design"},
    {"check_id": "receipt_data_vs_stored_values", "reason": "no PDF/receipt subsystem exists in this repository"}
  ],
  "summary": {"checks_passed": 11, "checks_failed": 2, "checks_not_applicable": 3, "total_violations": 7},
  "results": [
    {
      "check_id": "header_total_vs_denominations",
      "description": "Transaction.total_value must equal sum(Denomination.value) for its child rows",
      "severity": "high",
      "status": "fail",
      "violation_count": 3,
      "query_duration_ms": 38,
      "sample_violations": [
        {"transaction_id": "b7e1...", "total_value": "1250.00", "sum_denominations": "1240.00", "difference": "10.00", "business_date": "2026-08-19"}
      ]
    },
    {
      "check_id": "corrections_without_reasons",
      "description": "Every row with original_transaction_id set must have a non-empty correction_reason (see finding PG-5)",
      "severity": "high",
      "status": "pass",
      "violation_count": 0,
      "query_duration_ms": 4,
      "sample_violations": []
    }
  ],
  "exit_code": 1
}
```

`sample_violations` is capped at `--sample-size` (default 20) per check — `violation_count` always reflects the true total even when the sample is truncated, and every result carries a stable `check_id` so CI can diff runs and alert only on *new* violations landing in a previously-clean check.

### 4.4 Proposed check catalog (mapped to the brief's required list)

| check_id | Severity | Status now | Mechanism |
|---|---|---|---|
| `duplicate_bag_numbers` | High | Implementable | Group by the same normalization `duplicate_detection_service.py` already uses (case/whitespace-insensitive `bag_number`) within whatever scope the business confirms (PG-4) — flags rows the app-level heuristic in production may have missed (e.g. duplicates entered by two different users, which `check_for_duplicate`'s `user_id` filter never compares against each other) |
| `duplicate_transaction_references` | — | **Not applicable** — see §5 |
| `duplicate_idempotency_keys` | — | **Not applicable** — see §5 |
| `orphan_denominations` | Medium | Implementable | `Denomination` rows with no matching `Transaction` (should be structurally impossible given the FK, but SQLite FK enforcement is unconfirmed per PG-12 — useful as a live PostgreSQL FK-health signal too) |
| `missing_audit_events` | High | Implementable | Cross-references **both** `AuditLog` (catalog DB) and `CoreAuditLog` (core DB) per PG-11's sibling finding on split audit trails — e.g. every `is_superseded=True` transaction must have a matching `TRANSACTION_CORRECTED` entry; every `EODClosure` must have a matching `EOD_CLOSED` entry; every core `User` created must have a `USER_CREATED`-family entry |
| `invalid_state_transitions` | High | Implementable | Rows with `duplicate_flag_status` outside its known vocabulary; `balance_status`/`expected_total` combinations `calculate_balance_status()` could never produce; a row with both `is_superseded=True` and `original_transaction_id IS NOT NULL` simultaneously (would mean a corrected row was itself further "corrected" outside the guarded `correct_transaction()` path) |
| `completed_records_without_completion_metadata` | Medium | Implementable | `EODClosure` with `closed_automatically=False` and `closed_by_user_id IS NULL` (a manual close must have an actor); `DuplicateFlag.status != pending` with `reviewed_at`/`reviewed_by_user_id IS NULL` |
| `completed_records_changed_after_completion` | Medium | Best-effort only — see §5 | No `updated_at`/version column exists on any table (confirmed by full model read); can only correlate `AuditLog.timestamp` entries referencing a transaction against that transaction's `EODClosure.closed_at` for the same business_date/catalog as an indirect signal, never a direct one |
| `header_total_vs_denominations` | High | Implementable | `sum(Denomination.value)` per `transaction_id` vs `Transaction.total_value` — both `Numeric(12,2)`, so any nonzero difference is a real defect, not a rounding artifact |
| `batch_eod_totals_vs_transactions` | Medium | Implementable, but weaker than intended — see §5 | `EODClosure` has no stored total (no `closing_total`/`transaction_count` column exists on the model at all) — the check can only recompute `sum(Transaction.total_value)` per `business_date` live and report it, not compare against an independently-captured snapshot; flagged as its own schema gap |
| `receipt_data_vs_stored_values` | — | **Not applicable** — no receipt subsystem exists in this repository (confirmed, `00_baseline.md` §2) |
| `corrections_without_reasons` | High | Implementable | Rows with `original_transaction_id IS NOT NULL` and (`correction_reason IS NULL` or empty/whitespace-only) — directly backstops PG-5 |
| `restricted_actions_without_approval` | Medium | Narrow slice only | Verifies sensitive `AuditLog`/`CoreAuditLog` action types (`TRANSACTION_DAY_TRANSFERRED`, `EOD_REOPENED`, `USER_DELETED`, role-changing `USER_UPDATED`) always carry a non-null actor `user_id`; does **not** verify the actor held the required *role* at the time — that needs point-in-time role history, which doesn't exist (`User.role` is a current-value-only column). Full authorization-matrix enforcement is Agent 3's territory; this check is a narrow, DB-level spot-check only, not a duplicate of Agent 3's work |
| `records_attributed_to_missing_or_inactive_users` | High | Implementable, two-database design | Collects every distinct `user_id`-shaped column value referenced in the catalog DB (`Transaction.user_id`, `AuditLog.user_id`, `DuplicateFlag.reviewed_by_user_id`, `Notification.related_user_id`/`resolved_by_user_id`, `EODClosure.closed_by_user_id`/`reopened_by_user_id`), then does a **Python-side set difference** (no cross-database `JOIN` is possible — PG-13) against `SELECT id, is_active FROM users` read from `--core-database-url`. Reports two independent categories: `missing` (no matching core user row — always a hard violation) and `referenced_but_inactive` (resolves to a real user with `is_active=False` — reported as informational by default, since a user active at transaction-time and deactivated later is expected, not a defect; `User` has no `deactivated_at` timestamp so the checker cannot tell whether the deactivation happened before or after the referencing row — another schema gap worth noting for Phase 2) |
| `unexpected_gaps_from_partial_processing` | Medium | Heuristic only | UUID primary keys carry no sequence to find "gaps" in directly; instead: (a) `Transaction` rows with zero `Denomination` children (every real transaction is expected to have at least one); (b) business dates with transactions on both the day before and after but none on the day itself, per catalog+customer, as a "suspiciously empty day" signal |

---

## 5. What the checker **cannot** check yet — missing prerequisite features

Stated explicitly, as required:

1. **`duplicate_transaction_references`** — cannot be implemented. `Transaction` has no reference/business-number field of any kind; `transaction_id` (UUID4, PK) is the only identifier, and comparing UUIDs for "duplicates" is meaningless by construction. Blocked on Agent 1's reference-field design.
2. **`duplicate_idempotency_keys`** — cannot be implemented. Confirmed via `grep -ri idempot` across the entire repository: zero hits. No column, no table, no header, nothing. Blocked on Agent 1's idempotency design — and that design needs to settle whether an idempotency key is the *same* field as a future transaction reference or a genuinely separate concept (a client-generated request-dedup token vs. a human-facing business reference number are not necessarily the same field), since the checker's implementation differs depending on that answer.
3. **`receipt_data_vs_stored_values`** — not applicable, not just unimplemented. No PDF/receipt/label subsystem exists anywhere in this repository (confirmed independently by `00_baseline.md` §2 and re-confirmed here via the same full-repo search). The checker will report this as `not_applicable` with a reason string, not silently omit it, so a reader of the JSON output always sees *why* a required check from the brief is absent rather than wondering if it was forgotten.
4. **A true "changed after completion" check** — no table in the schema has an `updated_at`, a version/optimistic-lock column, or any row-level change-tracking (confirmed by a full read of every file in `backend/models/`). The proposed `completed_records_changed_after_completion` check (§4.4) can only correlate against `AuditLog` timestamps as an indirect proxy — it can say "an audit event touching this transaction happened after its business day closed," never "this specific row's contents changed after completion" with certainty, since nothing captures a before/after diff or even a last-modified timestamp on the row itself.
5. **A real batch/EOD-total mismatch check** — `EODClosure` stores no financial total or transaction count at closure time (confirmed: no such column on the model). The best the checker can do is recompute the total live and report it; it cannot detect "the total silently drifted after the day was closed," because there is no independently-captured value from closure time to compare against. This needs a schema addition (e.g. a `closing_total`/`closing_transaction_count` snapshot column, written once at `close_day()` time) before the check can do what the brief actually asks for.
6. **Full `restricted_actions_without_approval` enforcement** — the checker can only confirm a sensitive action's audit entry names a non-null actor; it cannot confirm that actor held the required role/permission *at the time* the action happened, because `User.role` is a current-value-only column with no history table. This is flagged, not solved, here — full authorization-matrix verification is Agent 3's domain and intentionally not duplicated in this checker.

---

## 6. Summary of confirmed-vs-assumed status

Every finding above is individually tagged; as a roll-up: **PG-1, PG-2 (SQLite/Postgres DDL divergence), PG-3 (absence of locking), PG-4, PG-5, PG-6, PG-8 through PG-14** are **Confirmed** by direct source reading, `grep`, or empirical SQLAlchemy engine/DDL introspection in the baseline venv (no live database, per Phase 1 constraints). **PG-2's forward-looking `ALTER TYPE` migration risk** and **PG-7 (raw-SQL `date()` dialect behavior)** are Confirmed as latent code-level facts but their real-world magnitude is **Assumed**, pending the Phase 5 checks listed in §3 against an actual running PostgreSQL instance.
