# 06 — Consolidated Production-Readiness Plan (Phase 2, Lead Coordinator)

Date: 2026-08-22
Author: Lead coordinator, synthesizing `01_transaction_integrity.md` (Agent 1),
`02_capacity_test_plan.md` (Agent 2), `03_security_review.md` (Agent 3),
`04_postgresql_and_reconciliation.md` (Agent 4), `05_resilience_and_operations.md`
(Agent 5), and `00_baseline.md`. No application code has been changed yet —
this document is the gate the brief requires before Phase 3 implementation
begins.

---

## 1. Confirmed current-state architecture

**Brink's Nexus**: FastAPI + SQLAlchemy async, dual-auth (JWT bearer for
`/api/v1/*`, `itsdangerous`-signed cookie for `/web/*`), five databases per
process — one core DB (users, `core_audit_log`) and four catalog DBs (VMS,
Dayshift, Complete, ESNF), each currently SQLite (aiosqlite), PostgreSQL
(asyncpg) supported per-database-URL but never exercised. Single `uvicorn`
process, `reload=True` hardcoded, no worker supervision. Every request-time
DB write goes through exactly one of two FastAPI dependency generators
(`get_core_db`/`get_catalog_db`, `backend/core/database.py:56-77`) that
commit once at the end of a successful request and roll back on any
exception — this is the one architectural invariant every agent's findings
lean on, and it holds (see §7).

Core domain: `Transaction` (UUID4 PK, `bag_number`, `total_value`,
`BalanceStatus`, `duplicate_flag_status`, append-only correction chain via
`original_transaction_id`/`is_superseded`/`correction_reason`), `Denomination`
(child rows that should sum to `total_value`), `EODClosure` (per-`business_date`
day-close, no monetary total stored), `DuplicateFlag`/`Notification` (soft,
post-insert duplicate detection), two separate audit tables (`AuditLog` per
catalog, `CoreAuditLog` in core). Migrations (1 core + 7 catalog) match the
models exactly — no drift (Agent 4 §2).

**Scope correction (binding for all phases below):** no PDF/receipt engine,
no label generation, no CSV import, no reprint flow, no generated-file
history exist anywhere in this repository (confirmed independently by all
five agents). Every plan item below is scoped to what actually exists.

## 2. Critical production blockers

Ranked by combined severity and independence (an item here blocks a safe
pilot regardless of the others):

1. **No idempotency mechanism for any state-changing write** (Agent 1 C-1,
   Agent 5 Critical #3). A lost response or client retry on
   `create_transaction` creates a second, fully-accepted financial row. The
   single most important gap in the whole assessment — it is the direct
   cause of "cannot prove duplicate financial effects are impossible,"
   which is the primary objective's second bullet.
2. **No unique constraint on `bag_number` at any scope**, and detection is
   same-user-only (Agent 1 C-2, Agent 4 PG-4). Two different couriers
   entering the same bag are invisible to the system today.
3. **Default credentials auto-seed into production on every empty-DB
   startup, and re-seeding silently resets a changed admin password back to
   `admin`** (Agent 3 S-01). Combined with:
4. **No startup validation of `SECRET_KEY`**, which signs both JWTs and web
   sessions (Agent 3 S-02). Together, #3+#4 mean a deployment that follows
   only the documented setup steps is compromised on day one, no attacker
   action required.
5. **No backup path works against PostgreSQL at all** (`IndexError` on any
   non-SQLite URL), **and no restore procedure exists for either engine**
   (Agent 4 PG-6, Agent 5 Critical #1/#2). A cash-of-record system with no
   tested recovery path.
6. **Zero connection-pool tuning across 5 engines**, invisible today only
   because SQLite silently no-ops it via `NullPool` (Agent 1 M-1, Agent 4
   PG-1, Agent 5 High #8 — corroborated by the Phase 0 baseline's own
   connection-leak `SAWarning`s). The first thing that breaks the moment
   PostgreSQL is adopted under real concurrency.
7. **Zero alerting/paging channel anywhere in the repo** (Agent 5 Critical
   #4). Every other item on this list is invisible to operations until a
   human happens to notice.

## 3. High-priority risks (not blocking a pilot by themselves, but required before general production use)

- No row-level locking / concurrency control anywhere; PostgreSQL's
  READ COMMITTED default will let races SQLite's file-level write
  serialization currently masks (Agent 1 H-2/H-3, Agent 4 PG-3).
- EOD-close race surfaces as an unhandled 500 instead of a clean 409 (Agent
  1 H-1, Agent 4 PG-3).
- `BalanceStatus.balanced`/EOD-closed status never gates mutation; a
  transaction can be transferred out of an already-closed day with no
  reopen (Agent 1 H-4).
- PostgreSQL native-enum divergence: a value SQLite has never rejected will
  hard-fail on first Postgres write (Agent 4 PG-2).
- No CSRF token on any web-portal POST (partially mitigated by
  `SameSite=Lax` only), no brute-force protection on login, no security
  headers, 14-day non-revocable session cookie vs. the JWT's 8-hour expiry
  (Agent 3 S-03/S-04/S-05/S-06).
- No structured logging, no correlation ID, `/health` checks nothing (Agent
  5 High #5/#6/#7).
- CSV-formula injection via `wallet_id` in report exports (Agent 3 S-07).
- No segregation-of-duties check — a supervisor can review their own
  duplicate flag or correct their own transaction (Agent 3 S-09).

Medium/Low findings from all five documents are not restated here; they
carry into the Phase 3/4 implementation backlog and the file-ownership map
in §14 unchanged from their source documents.

## 4. Dependency map between changes

```
SECRET_KEY validation (S-02) ──────────────┐
Auto-seed gate (S-01)  ─────────────────────┼─→ Phase 4 must land before ANY
Password policy (S-10) ─────────────────────┘   production deployment, independent
                                                  of everything else below.

Idempotency-key table (Agent 1 §6.1/§7)
        │
        ├─→ REST correction endpoint (Agent 1 M-2)   [needed so idempotency
        │                                              covers both surfaces]
        ├─→ integrity checker's duplicate_idempotency_keys check (Agent 4 §5.2)
        └─→ Agent 2's Idempotency-Key header in every load-test journey

Bag-uniqueness scope decision (business input, §16)
        │
        └─→ scoped UNIQUE constraint on Transaction (Agent 1 §6.2, Agent 4 PG-4)
                └─→ integrity checker's duplicate_bag_numbers check severity

version_id_col on Transaction/EODClosure (Agent 1 §8)
        │
        ├─→ StaleDataError → 409 handling in transaction_service.py/eod_service.py
        └─→ concurrency regression tests (Agent 1 §9)

IntegrityError → 409 translation on EOD close (Agent 1 H-1, Agent 4 PG-3)
        │  (independent, low-risk, can land first as a quick win)

Connection-pool tuning (Agent 4 PG-1 defaults)
        │
        ├─→ MUST land before any PostgreSQL load test (Agent 2 Workload A/B/D)
        └─→ pool-instrumentation decision (Agent 2 §12.2) informs /health redesign (Agent 5)

PostgreSQL migration execution (Agent 4 §3, checks 1-9)
        │
        ├─→ depends on: enum create_constraint decision, pool tuning, backup rewrite
        └─→ gates: Phase 5 step 6 (PostgreSQL test suite)

Backup/restore rewrite for PostgreSQL (Agent 4 PG-6, Agent 5 Scenario 12)
        │
        └─→ gates: Phase 5 step 14 (backup and restore test)

Structured logging + correlation ID (Agent 5 §1)
        │
        └─→ prerequisite for: meaningful /health, meaningful alerting (§2 item 7),
             and every "required support information" item in Agent 5's 12 scenarios
```

The practical reading: **Phase 4's credential/secret fixes are the only
items with no technical dependency on anything else** and should land
first regardless of implementation order chosen elsewhere. Everything else
funnels through either the idempotency-key design or the connection-pool
tuning before it can be meaningfully load- or PostgreSQL-tested.

## 5. Proposed database schema changes

Single, coordinated changeset (resolves the Agent 1 / Agent 4 file-overlap
noted in §14) — one migration series per catalog DB, in this order:

1. **`idempotency_keys` table**, one per catalog DB (Agent 1 §6.1):
   `(key TEXT PRIMARY KEY, catalog TEXT, scope TEXT, request_fingerprint TEXT,
   transaction_id UUID NULLABLE, created_at, expires_at)`.
2. **`Transaction.correction_reason`** — add
   `CHECK (original_transaction_id IS NULL OR (correction_reason IS NOT NULL
   AND correction_reason <> ''))` (Agent 4 PG-5).
3. **`UNIQUE` partial index on `Transaction.original_transaction_id`**
   (`WHERE original_transaction_id IS NOT NULL`) (Agent 1 §6.3).
4. **Scoped `UNIQUE` constraint on bag uniqueness** — placeholder
   `UNIQUE(customer_id, location_id, business_date, bag_number)` pending the
   business decision in §16; migration written but the constraint itself
   gated behind that sign-off (Agent 1 §6.2, Agent 4 PG-4).
5. **`version_id_col` (`Integer, nullable=False, default=1`) on `Transaction`
   and `EODClosure`** for optimistic locking (Agent 1 §8 — see §8 below for
   why this is chosen over row locking).
6. **`SAEnum(..., create_constraint=True)`** on every enum column
   (`BalanceStatus`, `User.role`, `Notification.severity/status`,
   `DuplicateFlag.status`, `EODClosure.status`) so SQLite dev/test finally
   enforces the same invariant PostgreSQL enforces natively, closing the
   coverage gap Agent 4 identified in PG-2.
7. **`ondelete="RESTRICT"`** on all FKs currently unspecified (Agent 4 PG-9)
   — matches this schema's append-only, audit-oriented design; a raw
   `DELETE` should fail loudly, not cascade or no-op silently.
8. **`server_default=text(...)` for UUID PKs** — deferred to new tables only
   (Agent 4 PG-8); not worth a backfill migration on existing tables for
   zero correctness benefit.

Each of these is independently reversible (a `downgrade()` dropping the
added constraint/column/table); none requires a data backfill except #4,
which needs a one-time dedup pass on existing data before the constraint
can be applied — **flagged as a real migration risk if this ever runs
against a populated production database**, and explicitly out of scope
until §16's decision is made.

## 6. Proposed transaction lifecycle

No change to the lifecycle *shape* Agent 1 documented (create → optional
correction, `BalanceStatus` and `duplicate_flag_status` as independent
tracks) — the gaps are in what gates transitions, not the states
themselves:

- `transfer_business_date`/`bulk_transfer_business_date` must check the
  transaction's **origin** `business_date` closed-status, not just the
  target (closes Agent 1 H-4), and `bulk_transfer_business_date` must
  re-check `is_day_closed` per row inside its loop, not once up front.
- `correct_transaction` gains the idempotency-key check (§7) and the
  self-correction guard (Agent 3 S-09: reject when
  `supervisor_user_id == original.user_id`).
- `DuplicateDetectionService.review_flag` gains the equivalent
  self-review guard (Agent 3 S-09).
- No new states are introduced. `BalanceStatus.balanced` remains
  informational (matching current design) unless the business decision in
  §16 changes that.

## 7. Proposed idempotency contract

Adopting Agent 1's design in full (`01_transaction_integrity.md` §7) as the
authoritative contract:

- Client-supplied `Idempotency-Key` header on `POST /api/v1/transactions/`
  and the new REST correction endpoint; a matching hidden form nonce for
  `wizard_complete` and `correct_transaction_submit`, generated once per
  draft/form-render, carried through resubmits, regenerated on a genuine
  page refresh.
- Server-side check-and-insert against `idempotency_keys` inside the same
  request-scoped session as the business write — both commit or both roll
  back as one unit, reusing the existing single-session-per-request
  boundary with no architectural change.
- A duplicate key within scope returns the original result (200), not a
  new insert.
- Key scope is `(catalog, endpoint, key)`, stored per-catalog DB (never the
  core DB), with a bounded expiry (24-48h) purged by the existing
  APScheduler infrastructure.
- Agent 2's load-test harness must send this header on every `create_transaction`
  call it makes and include a dedicated retry-safety workload case.
- Agent 4's integrity checker implements `duplicate_idempotency_keys` once
  this table exists (currently `not_applicable` in its design).

## 8. Proposed concurrency-control strategy

**Decision (resolves the one real disagreement between Agent 1 and Agent
4): optimistic locking via `version_id_col`, plus unique constraints for
insert-races, everywhere — never `SELECT ... FOR UPDATE`.**

Agent 4's PG-3 named `SELECT ... FOR UPDATE` as one option for the EOD-close
and duplicate-detection races; Agent 1 independently reached optimistic
locking for the update-race cases and a caught-`IntegrityError`-on-unique-constraint
for the insert-race cases, with a specific justification: SQLite (today's
only tested dialect) has no real row-level locking, so a `FOR UPDATE` clause
would be silently inert or dialect-dependent, giving false confidence in
tests that would not carry over to PostgreSQL. Optimistic locking and unique
constraints are pure application/schema logic that behaves identically on
both dialects. **Adopting Agent 1's strategy as the single answer**: it
subsumes Agent 4's insert-race remedies (c) and (b) exactly (unique
constraint + caught `IntegrityError` → the existing `ValueError`/409
pattern), and replaces remedy (a) — `SELECT FOR UPDATE` — with a version
column, because both a version column and a lock would solve the same
update-race problem, and only the version column is portable and testable
in this codebase's actual dev/CI environment (SQLite) without a false sense
of security. Agent 4's PG-3 finding itself (the races exist, here's where)
stands as written and is not contradicted by this decision — only the
proposed *mechanism* is resolved in Agent 1's favor.

Concretely: `Transaction.version_id_col` and `EODClosure.version_id_col`
(SQLAlchemy `__mapper_args__`), `StaleDataError` on `flush()`/`commit()`
caught and translated to 409, the same error-handling pattern already needed
for the EOD `IntegrityError` case — one pattern (`IntegrityError`/`StaleDataError`
→ 409) reused across both insert-race and update-race conflicts.

## 9. Proposed role/action matrix

Agent 3's matrix (`03_security_review.md`, Role/Action Permission Matrix
section) is adopted as-is for documentation purposes. Two changes required
to close confirmed gaps, both narrow and additive (no existing permission
loosened, per constraint #10 of the assessment mandate):

1. `GET /api/v1/transactions/{transaction_id}` tightened to
   `SupervisorOrAbove`, matching the web portal and the list endpoint
   (closes S-12), **unless** the business confirms cashiers should be able
   to look up their own past transactions — in which case an explicit
   ownership check (`txn.user_id == current_user.id`) replaces the tightening.
   This is a §16 decision, not a default assumption.
2. Catalog-crossing (S-08) is recorded as a confirmed *design choice*
   (README explicitly documents "switch catalogs at any time"), not
   silently changed — a §16 decision on whether catalog segregation is
   actually required.

No other row in the matrix changes; every other authorization check
already present is a genuine server-side dependency (Agent 3's positive
finding), so this phase is additive only.

## 10. Proposed audit guarantees

The strongest confirmed positive finding across all five documents: for
every request-time write path that goes through `get_core_db`/`get_catalog_db`
(all of `TransactionService`, `EODService`, `NotificationService`,
`DuplicateDetectionService`), the business write and its audit-log write
already share one session and one commit — Agent 1 §2 and Agent 5 §3 confirm
this independently by reading different code paths. **This guarantee is
preserved, not rebuilt** — Phase 3 must not introduce any new write path
that acquires its own session outside this pattern. The one confirmed
exception, `create_backup()` (filesystem) + its audit row (separate `core_db`
session), needs the two resources reconciled — proposed fix: write a
`BACKUP_ATTEMPTED` audit row *before* calling `create_backup()` (same
core_db session as the eventual success/failure row), then update it to
`BACKUP_SUCCEEDED`/`BACKUP_FAILED` after, so a crash mid-backup still leaves
a traceable record rather than silence.

New guarantee added by Phase 3: the idempotency-key insert (§7) lands in the
same session as the business write it protects, preserving this same
all-or-nothing property for the new mechanism, not just the old one.

## 11. Proposed PostgreSQL deployment design

Grounded entirely in Agent 4's findings, adopted as the reference design:

- Per-engine pool settings per Agent 4's PG-1 table (core:
  `pool_size=5/max_overflow=5/timeout=10s/recycle=1800s/pre_ping=True`;
  each catalog: `pool_size=10/max_overflow=10`, same timeout/recycle/pre_ping),
  as starting values to be tuned against Agent 2's real load-test numbers
  (Phase 5 step 7 in Agent 4's checklist) — not treated as final.
- Evaluate PgBouncer in transaction-pooling mode before any multi-worker
  deployment, with the `asyncpg` + PgBouncer transaction-mode caveats
  (no session-level prepared statements, no advisory locks across
  transactions) explicitly validated in Phase 5, not assumed.
- `alembic upgrade head` (not `create_all` + stamp) becomes the fresh-install
  path once Postgres is the target — the current `init_databases()` shortcut
  is confirmed safe only because it's never been diffed against a real
  migration run (Agent 4 Phase 5 check #4); this diff must happen before
  the shortcut is trusted on Postgres.
- `backup_service.py` is rewritten around `pg_dump`/`pg_restore` subprocess
  calls (never an in-process DB-API call, which has no Postgres equivalent
  for what `sqlite3.Connection.backup()` does today), with a genuine
  restore-and-validate step (restore to a scratch DB, run the integrity
  checker against it) added for **both** engines — SQLite has never had
  this either (Agent 4 PG-6, Agent 5 Scenario 12).
- The 9 PostgreSQL-specific checks in Agent 4 §3 (migration-from-empty,
  enum rejection, downgrade/upgrade round-trip, `create_all` vs. real
  migration diff, concurrent-EOD-close load test, FK enforcement, pool
  sizing from real numbers, dump/restore cycle, session-timezone
  divergence) become the literal Phase 5 PostgreSQL test suite — no new
  test design needed, only execution.

## 12. Test strategy

Layered, matching Phase 5's ordering in the original mandate:

1. **Unit/integration** — existing 127-test suite stays green throughout;
   new tests added per control area, never replacing an existing assertion
   to make it pass (constraint #10).
2. **Transaction invariant / concurrency** — Agent 1 §9's 10 proposed test
   names, run first against SQLite (fast feedback), then the subset that is
   dialect-sensitive (H-1 through H-3, all locking behavior) re-run against
   PostgreSQL once Phase 3's schema lands.
3. **Authorization matrix** — Agent 3's proposed
   `test_full_role_matrix_against_every_route`, run as a **live** test
   against a real spawned server (not in-process ASGI), parametrized over
   every route × every role × anonymous.
4. **PostgreSQL suite** — Agent 4 §3's 9 checks, executed in the listed
   order (each has explicit dependencies on the ones before it).
5. **Load/capacity** — Agent 2's workload profiles A→F in the order given;
   never skip ahead to a more expensive profile if an earlier one shows a
   transaction-invariant failure (per the mandate's Phase 5 ordering rule).
6. **Failure injection** — Agent 5's 12 scenarios, each with its own
   pass/fail evidence definition already specified in that document; run
   only after the app has the idempotency/locking/pool-tuning fixes,
   otherwise every scenario just re-confirms Phase 1's findings instead of
   validating a fix.
7. **Soak + backup/restore** — last, since they're the most time-expensive
   and least likely to reveal anything not already caught by 1-6, per the
   original mandate's own sequencing.

The integrity checker (Agent 4 §4) runs as a **post-run reconciliation
step after every one of the above categories that writes data**, not just
at the end — this is what turns "the test passed" into "the test passed
and left the database in a provably correct state."

## 13. Rollback strategy

- Every schema migration in §5 ships a working `downgrade()`; none are
  destructive to existing data except the scoped bag-uniqueness constraint
  (§5 item 4), which requires the dedup pass to be reversible-in-spirit
  (the dedup itself is not undoable, but the constraint can be dropped).
- Application-level changes (idempotency plumbing, locking, self-review
  guards, security hardening) are additive checks on existing code paths,
  not rewrites — each can be reverted independently by reverting its own
  commit, since Phase 3/4 commits are scoped by control area (per the
  mandate's commit strategy), not bundled.
- Security defaults (§16 decisions on secret-key validation, auto-seed
  gating) must ship behind an explicit production-mode flag that defaults
  to today's permissive dev behavior in development, and can be disabled
  in an emergency (e.g. a broken secret-key check locking out a real
  deployment) by unsetting that one flag — never by weakening the
  underlying check itself.
- PostgreSQL adoption is opt-in per `DATABASE_URL_*` — SQLite remains fully
  supported for development/pilot per the mandate's constraint #8, so a
  failed Postgres cutover can fall back to SQLite without a code change.

## 14. File ownership by agent (Phase 3+ implementation)

Unlike Phase 1's investigation (five independent read-only agents, no file
overlap by construction), Phase 3+ implementation has **real overlap** on
two files: `backend/models/transaction.py` and `backend/models/eod.py`
each carry schema changes proposed by both Agent 1 and Agent 4. Per the
mandate ("Do not let multiple agents edit the same files"), ownership is
reassigned by **control area**, not by which agent originally found the
issue:

| Control area | Owns | Files |
|---|---|---|
| Schema & migrations (idempotency table, version columns, unique constraints, CHECK constraints, enum `create_constraint`, FK `ondelete`) | Single owner (lead-designated implementer) | `backend/models/transaction.py`, `backend/models/eod.py`, new `alembic_catalog/versions/*.py` |
| Idempotency + concurrency application logic | Same owner as schema (tightly coupled — see §4 dependency map) | `backend/services/transaction_service.py`, `backend/services/eod_service.py`, `backend/repositories/transaction_repository.py`, `backend/repositories/eod_repository.py`, `backend/api/routes/transactions.py` (new correction endpoint), `backend/api/routes/eod.py`, `web/routes/transaction_entry_web.py`, `web/routes/transactions_web.py` |
| Security/secrets/auth | Independent owner | `backend/core/config.py`, `backend/core/security.py`, `backend/main.py` (startup validation, security headers, CSRF), `database/seed.py`, `backend/schemas/user.py` (password policy) |
| Self-review/segregation-of-duties | Same owner as security (small, additive checks in the services the schema/concurrency owner already touches — coordinate, don't duplicate edits) | `backend/services/duplicate_detection_service.py`, `backend/services/transaction_service.py` (coordinate with the concurrency owner on `correct_transaction`) |
| PostgreSQL deployment + connection pooling + backup/restore rewrite | Independent owner | `backend/core/database.py`, `backend/services/backup_service.py`, new restore tooling |
| Integrity checker | Independent owner, depends on schema owner's idempotency table landing first for full coverage | New `tools/integrity_check/` package |
| Load-test harness | Independent owner, depends on idempotency header contract (§7) being finalized before writing the create-transaction workload | New `tools/loadtest/` package (name TBD in Phase 3) |
| Resilience (logging, correlation ID, health/readiness, alerting hooks) | Independent owner | `backend/main.py` (shared with security owner — coordinate on the same file), new logging config |

Where two areas touch the same file (`backend/main.py`,
`backend/services/transaction_service.py`), the two owners integrate
sequentially through the lead rather than editing concurrently — the lead
coordinator merges, per the mandate's explicit reservation of "shared
interfaces" and "integration of agent work."

## 15. Estimated complexity (not calendar time)

| Item | Complexity | Why |
|---|---|---|
| Secret-key/auto-seed/password-policy fixes | **Small** | Additive startup checks, no schema change, well-isolated |
| Idempotency table + header/nonce plumbing | **Medium** | New table + new logic on 4 call sites (2 API, 2 web), but the session-boundary pattern already supports it cleanly |
| Version columns + IntegrityError/StaleDataError handling | **Medium** | Schema change + error-translation logic across 2 models, well-precedented pattern |
| Scoped bag-uniqueness constraint | **Small once §16 is decided, blocked until then** | The constraint itself is trivial; the blocker is a business decision plus a dedup pass if data already exists |
| CSRF + security headers + rate limiting | **Medium** | Touches every POST route in `web/routes/*.py`; mechanically repetitive but not conceptually hard |
| Self-review/segregation-of-duties guards | **Small** | Two narrow checks in two existing service methods |
| PostgreSQL pool tuning + enum constraint + FK ondelete | **Small** | Configuration and declarative model changes, no new logic |
| Backup/restore rewrite for PostgreSQL | **Large** | New subprocess-based mechanism, no existing pattern to extend, needs its own test infrastructure (scratch DB, restore validation) |
| Integrity checker | **Large** | New standalone tool, ~13 distinct checks, cross-database logic for the user-existence check (PG-13) |
| Load-test harness + all 6 workload profiles | **Large** | New tool + substantial run-time (soak alone is 2-4h minimum per Agent 2 §5) |
| Logging + correlation ID + real `/health` + alerting hooks | **Medium-Large** | Touches every route indirectly (middleware-level), but the pattern is uniform once designed once |
| Authorization-matrix live test | **Medium** | Mechanical once the matrix is finalized, needs a real spawned server (not ASGI-in-process) |

## 16. Decisions that require business input

Collected from all five documents — nothing below has a default assumed
without saying so explicitly:

1. **Bag-uniqueness scope** (Agent 1 C-2, Agent 4 PG-4): is a bag number
   unique per customer+location+business_date (the working assumption in
   §5), globally unique, or can it legitimately recur in ways that
   assumption would wrongly reject? The wizard's own "next wallet, same
   bag" flow already creates multiple rows sharing one bag number within a
   day, so the naive `UNIQUE(bag_number)` is confirmed wrong — the actual
   correct scope needs the business's operational definition of "duplicate
   bag," not an engineering guess.
2. **Catalog segregation** (Agent 3 S-08): is any authenticated user being
   able to reach all 4 catalogs' data intentional (matches current
   README-documented behavior) or does the business actually require
   per-user catalog assignment?
3. **Self-transaction visibility via the API** (Agent 3 S-12): should a
   cashier be able to look up their own past transactions via the JSON API
   (currently possible, inconsistent with the web portal), or should the
   API match the web portal's supervisor-only restriction?
4. **Concurrent-user capacity assumption** (Agent 2 §4): every load-test
   threshold in `02_capacity_test_plan.md` scales from an assumed ~25-50
   concurrent active sessions peak (tens of cashiers/supervisors per
   shift, one deployment). If a real Nexus deployment serves multiple
   sites/shifts concurrently at a materially higher number, every workload
   tier needs re-scoping before its results are authoritative.
5. **Realistic workflow mix weighting** (Agent 2 §5 Profile C): the
   65/15/5/8/4/3 journey-weight split is a plausible guess, not measured
   usage — replace with real usage data if any exists.
6. **Password policy strength** (Agent 3 S-10): what minimum
   length/complexity is actually required for cash-handling staff
   accounts? A recommendation (10-12+ chars, mixed classes) is offered but
   is not a business decision this document can make unilaterally.
7. **Segregation-of-duties enforcement strength** (Agent 3 S-09): should a
   supervisor be flatly blocked from reviewing/correcting their own work
   (the proposed default), or should this instead require a second
   administrator override rather than an outright block, for cases where a
   small deployment genuinely has only one supervisor on shift?
8. **DB connection-pool instrumentation approach for load testing** (Agent
   2 §12.2): a temporary debug endpoint (needs an explicit "never ships to
   production" guard) or inference from error patterns only? This affects
   how much confidence the load-test numbers can have about pool behavior
   specifically.

---

## 17. What Phase 2 concludes

The plan above is internally consistent: every cross-agent overlap
identified in this synthesis (idempotency, bag uniqueness, connection
pooling, the optimistic-locking-vs-row-locking question, the shared
`backend/models/transaction.py` file, the shared `backend/main.py` file,
denomination reconciliation, backup/restore) has one resolved answer, not
two competing ones. Nothing in Phase 1 found a reason to believe the
architecture itself is wrong — the atomic single-session-per-request
pattern, the append-only correction design, and the multi-catalog database
separation are all sound and are preserved, not rebuilt. The gaps are
uniformly *missing controls on top of* a reasonable architecture, not
architectural flaws requiring a rewrite.

Per the mandate, broad implementation (Phase 3 onward) does not begin
automatically from here — see the message accompanying this document for
the specific checkpoint being raised before that work starts.
