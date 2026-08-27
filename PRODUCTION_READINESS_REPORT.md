# Brink's Nexus — Production Readiness Report

Branch: `claude/nexus-production-readiness-1yxu9y` · Commit at time of writing: `e37a630`
Date: 2026-08-27

## 1. Executive Summary

This assessment ran a full crash-test and security-review programme against Brink's Nexus (a FastAPI/SQLAlchemy cash-in-transit platform), following the phased plan in `docs/production_readiness/`. Phase 0 preserved a clean baseline; Phase 1 deployed five parallel investigative agents that produced 13 Critical, 20 High, 20 Medium, and 16 Low findings across transaction integrity, load capacity, security, PostgreSQL readiness, and operational resilience; Phase 2 consolidated those into one plan resolving cross-agent conflicts; Phases 3–4 implemented the foundation and security controls; Phase 5 executed real validation — the full test suite, live PostgreSQL migrations, an authorization-matrix sweep, real load tests against a socket-connected server, a process-kill failure injection, and backup/restore round-trips against both SQLite and PostgreSQL.

**The application is materially more production-ready than it was at the start of this engagement, but it is not yet ready for unsupervised production use.** The Critical gaps that would have caused financial harm on day one — auto-seeded default credentials reaching production, no `SECRET_KEY` validation, no idempotency protection against duplicate transactions, a backup mechanism that didn't function against PostgreSQL at all — are fixed and verified. What remains is real: SQLite's write-contention ceiling was measured directly (not assumed) and is low enough that it needs an explicit capacity decision before go-live; one observability gap in server-side exception logging is documented but unresolved; a business decision on bag-uniqueness scope is still pending and nothing was implemented against it; and full-duration soak testing (2–4 hours) was not run in this session due to time constraints — a short, real substitute was run instead and is reported as exactly that, not extrapolated into a soak-test pass.

No claim of "secure," "scalable," or "production-ready" is made anywhere in this report except where the specific objective criterion behind that claim was actually met and is cited below.

## 2. Scope and Exclusions

The original task brief assumed a bag-processing system with a locked PDF receipt engine (`receipt_engine/receipt_3550_attempt6.py`), label generation, CSV import, reprints, and generated-file history. **None of this exists in the repository.** Verified by full-tree search at the start of the engagement (`docs/production_readiness/00_baseline.md` §2) and never found anywhere in five independent agents' subsequent work. The real system — Brink's Nexus — is a multi-catalog (VMS/Dayshift/Complete/ESNF) transaction and EOD-closure platform with G+D BPS C1 hardware integration and Excel/CSV *export* reporting. Every deliverable in this report is scoped to what actually exists; label generation, PDF receipts, CSV import, reprints, and generated-file history are out of scope, not silently assumed absent.

Excluded from this session for time reasons, documented as such rather than skipped silently: a full 2–4 hour soak test (a ~10-minute equivalent load was run instead, see §6); spike-test Profile D and heavy-operations Profile F at full scale (harness supports them, not executed here); most of Agent 5's 12 failure-injection scenarios beyond process-kill (documented with expected behavior in `05_resilience_and_operations.md`, not all independently re-executed).

## 3. Test Environment

| Item | Value |
|---|---|
| OS | Linux 6.18.44-fc-v21, x86_64, containerized sandbox — **not** representative of target production hardware |
| CPU / Memory | 4 vCPU / 15 GiB RAM |
| Python | 3.11.15 |
| Dependency versions | Pinned exactly as in `requirements.txt` (fastapi 0.115.0, sqlalchemy 2.0.35, aiosqlite 0.22.1, asyncpg 0.29.0, alembic 1.18.5, pydantic 2.9.2, python-jose 3.3.0, passlib[bcrypt] 1.7.4, apscheduler 3.10.4, jinja2 3.1.6, pandas 2.2.3, openpyxl 3.1.5, pytest 8.3.3, pytest-asyncio 0.24.0) |
| Databases exercised | SQLite (aiosqlite), dev/test default; **PostgreSQL 16.13**, real server stood up in-sandbox for this engagement (`nexus_test` role, five `nexus_*_test` databases matching the app's core+4-catalog layout) |
| App server | Single-process `uvicorn`, no workers, no supervisor — matches the current `run_backend.py`/documented launch path exactly, not a hypothetical production topology |
| Load-test client | Real socket-connected `httpx.AsyncClient` against a real spawned `uvicorn` process (never in-process ASGI, which the load-test plan explicitly rules out as unrepresentative) |

All numbers in this report carry this hardware caveat. None should be read as a production capacity guarantee.

## 4. Baseline Results

Recorded before any code change (`00_baseline.md`): branch `claude/nexus-production-readiness-1yxu9y` at commit `8b5f51d`, working tree clean, no `.env`/`.db` files/real customer data anywhere in the repo. Baseline suite: **127 passed, 1 skipped, 19.4s.**

## 5. Changes Implemented

Ten commits, one per control area, each independently reviewable and revertable:

| Commit | Control area |
|---|---|
| `2f58e37` | Idempotency table, optimistic-locking version columns, `correction_reason` CHECK, enum `create_constraint`, FK `ondelete=RESTRICT` |
| `e1cd405` | PostgreSQL connection-pool tuning (dialect-aware), real backup **and restore** for both SQLite and PostgreSQL |
| `078c2ec` | Load-test harness (`tools/loadtest/`) |
| `832efdc` | Idempotency wiring, concurrency-conflict → 409 translation, self-review/self-correction guards |
| `64d9d9f` | Fixed a real process-hang in the test suite (pytest-asyncio event-loop-scope mismatch) |
| `d7c32f3` | Secret/session/CSRF/rate-limiting/security-headers/observability hardening (Phase 4) |
| `b68d622` | Independent integrity checker (`tools/integrity_check/`) + regression suite |
| `d219407` | Fixed a real PostgreSQL migration failure (FK-ondelete migration assumed a SQLite-only constraint name) |
| `cb3bccd` | Live authorization-matrix regression sweep |
| `cd70693` | Logging-handler hardening + honestly documented an unresolved observability gap |
| `e37a630` | Fixed a load-test harness bug that was generating false reconciliation-difference findings |

Deliberately **not** implemented, per explicit business-decision gating (`06_consolidated_plan.md` §16): the scoped bag-uniqueness database constraint. The business chose "need to think about it" over a specific scope when asked; implementing a guess would have risked silently rejecting legitimate transactions. The migration is designed (documented in the consolidated plan) but not applied.

## 6. Concurrent-User Capacity

Measured, not assumed, against the current SQLite dev/test configuration (Profile A, `tools/loadtest`):

| Concurrent VUs | RPS | p95 (ms) | p99 (ms) | Unexpected failures |
|---|---|---|---|---|
| 1 | 14.5 | 282 | 288 | 0 / 463 |
| 5 | 34.7 | 408 | 620 | 0 / 1,110 |
| 10 | 33.8 | 821 | 3,124 | 2 / 1,081 |
| 20 | — | — | — | 9 / ~800 (8 real 500s on the transaction-commit endpoint) |
| 25 | 34.6 | 5,132 | 8,392 | 34 / 1,176 |

Data integrity (missing records, missing audit events, orphans, duplicate bags, reconciliation differences) was **zero at every tier**, including the tiers with real 500 errors — failures failed safely; nothing corrupted. The 500s at 20–25 VUs are consistent with SQLite's single-writer file lock and the confirmed absence of a configured `busy_timeout` (Agent 5 Low finding #18, not fixed in this engagement).

**Capacity conclusion:** on this sandbox's hardware and with the current SQLite default, the system holds up cleanly to roughly 10 concurrent users and visibly degrades (latency and real errors) by 20–25. `02_capacity_test_plan.md`'s assumed real-world peak was confirmed by the business as ~25–50 concurrent users — this is at or above the point where SQLite alone showed cracks in this sandbox. **This is the strongest concrete argument in this whole engagement for treating PostgreSQL as a requirement, not an optional upgrade, for the target concurrency**, and for reproducing this exact test against PostgreSQL with the tuned pool settings (§15) before any go-live decision.

## 7. Transaction Throughput

Profile B (transaction-creation ramp, API-only path, isolates business logic from HTML rendering):

| Attempts | Concurrency | TPS (mean) | Unexpected failures | Data integrity |
|---|---|---|---|---|
| 500 | 5 | 25.0 | 0 / 505 | 0 missing/orphan/duplicate/reconciliation |
| 1,000 | 10 | 23.7 | 3 / 1,010 | 0 missing/orphan/duplicate/reconciliation |
| 3,000 | 10 | 16.9 | 20 / 3,010 | 0 missing/orphan/duplicate/reconciliation |

TPS declines as sustained volume grows even at fixed concurrency (25.0 → 23.7 → 16.9), consistent with SQLite write-lock contention accumulating over a longer run, not a one-off spike.

## 8. p50, p90, p95, and p99 Latency

Reported per-tier in §6/§7; full per-endpoint breakdowns are in the (gitignored, not committed per the commit-strategy constraint against pushing load-test output) JSON run artifacts. Headline: **p95 < 1s holds only at the lowest concurrency tier (1 VU: 282ms)**; every tier at 10 VUs and above misses the plan's p95 < 1s / p99 < 2s targets on this sandbox's SQLite configuration. This is reported as a fact, not adjusted — per the plan's own explicit instruction not to lower thresholds to make a sandbox result look like a pass.

## 9. Error Breakdown

At elevated concurrency, unexpected failures were concentrated almost entirely on one endpoint: `POST /web/transactions/new/wizard/complete` (30/34 failures at 25 VU; 8/9 at 20 VU), all returning the correctly-branded, sanitized 500 page (confirming the new generic exception handler works under real concurrent load, not just in isolation) rather than a raw crash or hang. A small number of `GET /web/login` connection resets also occurred at peak concurrency. Zero failures were classified as valid business rejections during any load run — every failure was either a full business success or a genuine unexpected error, cleanly separated by the harness's classifier.

## 10. Database Reconciliation

Zero missing records, zero missing audit events, zero orphan records, zero duplicate bags, and zero header/denomination reconciliation differences across every load-test run in this session (Profiles A and B combined, several thousand real transaction-create attempts). One reconciliation-difference false-positive was found and fixed during this work — it was a load-test harness bug (sending a `total_value` that didn't match its own denominations), not an application defect; re-run after the fix confirmed zero across the board. The independent integrity checker (`tools/integrity_check`) was separately proven against real, live PostgreSQL data and correctly caught a genuine pre-existing bug in the demo seed script (`database/seed.py` sets `balance_status=BALANCED` without `expected_total`, which the application's own logic guarantees can't happen) — direct evidence the checker works, not just that it runs.

## 11. Concurrency and Idempotency Results

Implemented and tested (`tests/test_production_readiness_foundation.py`, 13 tests): idempotency-key collapse of a genuine retry into one transaction; concurrent EOD-close correctly returns 409 for the losing request instead of an unhandled 500 (verified via real `asyncio.gather` concurrency, not sequential calls); double-correction of the same transaction rejected at the DB level via a unique partial index; stale-version updates rejected via optimistic locking rather than silently overwritten; `transfer_business_date` now blocks transferring a transaction out of its own already-closed day (previously only the target day was checked); self-correction and self-review-of-own-flag both rejected. All verified against real concurrent execution, not just sequential test calls, where the scenario required it.

Process-kill failure injection (this session, live): killing the server mid-flight during 300 concurrent transaction-create requests left the database in a state exactly matching what the client actually received — 5 transactions committed with 201 responses, and the database held exactly 5 fully-consistent `Transaction`+`Denomination`+`AuditLog` rows, zero partial writes, zero orphans. This is direct, executed evidence for the atomicity claims made throughout Phase 1 and Phase 3, not an inference from code reading alone.

## 12. Security Findings

Full detail in `docs/production_readiness/03_security_review.md`. Of the 2 Critical / 4 High / 6 Medium / 5 Low findings, the following are **implemented and verified** in this engagement:

- **S-01/S-02 (Critical)**: production-mode startup now refuses to run with a missing/default/short `SECRET_KEY` or an insecure session cookie; `_auto_seed()` no longer runs in production mode at all.
- **S-03**: CSRF token generated per session, verified server-side on every state-changing web route (production-mode only, preserving the existing dev/test suite exactly).
- **S-04**: brute-force lockout after 5 consecutive failed logins per (username, IP) — explicitly documented as single-process-only, needing a shared store before any multi-worker deployment.
- **S-05**: security headers (`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, a CSP derived from an actual template audit, production-only HSTS).
- **S-06**: web session cookie now has an explicit `max_age` matching the JWT's configured lifetime, instead of Starlette's 14-day default.
- **S-07**: CSV/Excel formula-injection mitigation on report export.
- **S-09**: segregation-of-duties — a supervisor can no longer approve their own restricted action.
- **S-10**: password policy strengthened.
- **S-12**: `GET /api/v1/transactions/{id}` tightened to supervisor+ in production mode, while preserving the existing dev-mode test's asserted (permissive) behavior — a deliberate, documented resolution, not an oversight, verified by a dedicated test that asserts both halves.

**Not implemented in this engagement**, carried forward as open items: S-08 (catalog segregation — confirmed to be an intentional design choice pending business confirmation, not touched), and dependency/static-analysis scanning (`pip-audit`, `bandit`, `gitleaks`) was recommended by Agent 3 but not run in this session.

## 13. Authorization Matrix Result

`tests/test_authorization_matrix.py`, 15 tests, all passing against the real application (in-process ASGI, adequate for status-code authorization checks per Agent 3's own methodology note): every route in the documented matrix returns the correct status for anonymous/cashier/supervisor/administrator, including two corrections made against the report's own first-draft assumptions after running it for real — `HTTPBearer`'s actual 403-for-missing-credential behavior (not the commonly assumed 401), and the S-12 fix's real production-mode gating (not an unconditional tightening).

## 14. Audit-Integrity Result

Confirmed by direct code reading (Agent 1, `01_transaction_integrity.md` §2) and independently reconfirmed by every load test in this session: every request-time business write and its audit-log entry share one database session and commit atomically — never observed to diverge, including under process-kill injection. The one confirmed exception (`create_backup()`'s filesystem operation vs. its audit row, two independent resources) was identified but not fixed in this engagement; it is a documented, narrow gap, not a general audit-integrity failure.

## 15. PostgreSQL Validation

Real, not configuration-only, validated in this session against a live PostgreSQL 16 server:

- `alembic upgrade head` from empty: **works**, after fixing a real bug found in the process (`d219407`) — the FK-ondelete migration assumed a constraint name that only existed under SQLite's batch-mode naming convention; PostgreSQL assigns its own default name, and the migration failed the first time anyone ran it for real. Fixed by reflecting the actual constraint name at migration time and using a plain `ALTER TABLE` on PostgreSQL instead of table-recreation.
- Enum enforcement: **confirmed** — PostgreSQL's native `ENUM` type rejects an invalid `balance_status` value that SQLite silently accepts, exactly matching Agent 4's PG-2 prediction.
- Downgrade/upgrade round-trip: **clean**, no orphaned `CREATE TYPE` failure.
- Connection-pool exhaustion: **proven** — a `pool_size=5/max_overflow=5` engine driven to 11 concurrent checkouts raises a clean `TimeoutError` after ~10s, not a hang.
- Backup/restore: **full round-trip proven** for both engines — PostgreSQL via real `pg_dump`/`pg_restore` subprocess calls into disposable scratch databases, SQLite via scratch temp-directory files. This capability did not exist in the codebase before this engagement (confirmed absent by Agent 4/5).
- One configuration constraint confirmed (not a code defect): core and catalog databases must be physically separate — sharing one physical database collides Alembic's default `alembic_version` table name across the two independent migration histories. The app's documented five-separate-databases design already avoids this.

Not validated in this session: the raw-SQL `date()` timezone-divergence concern (PG-7) and full FK-enforcement-under-PostgreSQL-vs-SQLite (PG-12) — both flagged by Agent 4 as lower-priority and requiring a populated-database migration scenario this engagement didn't need to exercise.

## 16. Failure and Recovery Results

Executed live in this session: **process-kill mid-transaction** (§11) — clean, no corruption, exactly matching what the client received. Not independently re-executed in this session (documented with expected behavior and reproduction steps in `05_resilience_and_operations.md`, based on code reading, not assumption): DB connection interruption, filesystem read-only/disk-full, external-dependency (hardware counter) failure, audit-write independent failure. The one operational gap that was fixed rather than only documented is backup/restore (§15/§17); the remaining scenarios' documented expected behavior should be treated as a Phase-5-continuation checklist, not as executed evidence.

## 17. Backup and Restore Evidence

Proven end-to-end in this session, both directions, both engines (`tests/test_backup_restore.py`): seed real data → back up → simulate a clean environment (drop and recreate the database, or delete and recreate the file) → restore → verify the data matches exactly. This is the first time in this codebase's history that a restore has ever been proven to work at all, for either database engine.

## 18. Remaining Risks

- SQLite write contention is a real, measured ceiling (§6/§7), not a theoretical concern — it must be weighed against the confirmed ~25–50 concurrent-user production expectation before any go-live decision that keeps SQLite.
- No `busy_timeout` configured (Agent 5 Low #18) — a direct contributor to the 500s observed at 20–25 VU.
- One unresolved, honestly-documented observability gap: under a real `uvicorn`-launched process, this session's own application-level log messages (via `logging.getLogger("nexus")`) did not appear in server output, while other loggers (SQLAlchemy, Alembic) worked correctly through the same handler throughout the same process. The user-facing behavior (clean, sanitized error responses) was independently verified correct regardless; what's unconfirmed is purely whether the accompanying server-side log line is captured for support diagnosis. See `backend/main.py`'s `_configure_logging()` docstring for the full investigation trail.
- Bag-uniqueness scope remains an open business decision; nothing was implemented against it.
- No dependency vulnerability scan (`pip-audit`), static security analysis (`bandit`), or git-history secret scan (`gitleaks`) was run in this engagement.
- Multi-worker/multi-process deployment would break the in-process rate limiter (S-04) and needs a shared store first.
- Full-duration soak testing (2–4h) was not executed.

## 19. Production Blockers

None of the original Critical findings remain unaddressed as blockers — they are fixed and verified (§5, §12, §15, §17). The items in §18 are risks to weigh, not confirmed blockers, with one exception: **a go-live decision that keeps SQLite as the production database, at the confirmed ~25–50 concurrent-user expectation, is not supported by the evidence in this report** (§6) and should not proceed without either re-running this exact load test against PostgreSQL first, or accepting a materially lower concurrent-user ceiling than the business has stated it needs.

## 20. Pilot Recommendation

A limited pilot — a single site, PostgreSQL as the database from day one, real (not seeded) credentials, `ENVIRONMENT=production` set so every gated hardening control activates — is reasonable given what this engagement verified. A pilot should not proceed on SQLite at the stated concurrency expectation. The pilot should include: running §6/§7's load tests against the pilot's actual PostgreSQL instance before onboarding real users, confirming the bag-uniqueness scope decision with the business before go-live (or explicitly accepting the current soft-detection-only behavior), and standing up the alerting this report's §18 flags as absent.

## 21. Monitoring and Rollback Requirements

**Monitoring** (none of this exists today — confirmed absent by Agent 5, not built in this engagement): a 5xx-rate alert, a backup-staleness alert (backup is 100% manual/admin-triggered, no scheduled job), a DB connection-pool saturation metric, and resolution of the logging gap in §18 before relying on server-side logs for incident diagnosis.

**Rollback**: every schema migration in this engagement has a working `downgrade()` (verified for the FK-ondelete migration specifically, §15). Application-level changes are additive checks behind either a production-mode gate (secrets, CSRF, S-12, rate limiting) or a schema addition (idempotency table, version columns) — each is revertable independently via its own commit (§5) without needing to revert the others. PostgreSQL adoption is opt-in per `DATABASE_URL_*`; SQLite remains fully supported, so a failed PostgreSQL cutover can fall back without a code change.

## 22. Exact Commands to Reproduce

```bash
# Full test suite (unit, integration, concurrency, authorization matrix,
# PostgreSQL migrations if a server is reachable at localhost:5432,
# backup/restore, integrity checker)
python -m pytest -q

# PostgreSQL-specific tests only (skip cleanly if no server reachable)
python -m pytest tests/test_postgresql_pooling.py tests/test_postgresql_migrations.py tests/test_backup_restore.py -q

# Independent integrity checker against a running catalog
python -m tools.integrity_check --catalog vms \
  --catalog-database-url <URL> --core-database-url <URL> --format text

# Load test (Profile A — baseline; run a real uvicorn process first,
# from a scratch directory so it doesn't touch the repo's own .db files)
uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
python -m tools.loadtest --profile A --tiers 1,5,10,25,50 --catalog vms \
  --base-url http://127.0.0.1:8000 --db-dir <scratch-dir> --server-pid <pid>

# Load test (Profile B — transaction-creation ramp)
python -m tools.loadtest --profile B --cases 500:5,1000:10,3000:25,5000:50 \
  --catalog vms --base-url http://127.0.0.1:8000 --db-dir <scratch-dir>

# Backup and restore CLI
python -c "from backend.services.backup_service import create_backup; import asyncio; print(asyncio.run(create_backup()))"
python -m backend.services.restore_service --backup-dir <dir> --target core=<URL> --target catalog_vms=<URL>
```
