# 00 — Baseline (Phase 0)

Date: 2026-08-22
Recorded by: Lead coordinator (Claude)

## 1. Branch and commit

- Branch: `claude/nexus-production-readiness-1yxu9y`
- HEAD commit: `8b5f51d` — "Merge pull request #2 from Retype32/claude/cash-system-features-z0z699"
- Working tree at start of session: clean, no untracked files, no local modifications.
- Remote: `origin` = `https://github.com/Retype32/test`
- No other stashes or in-progress work found (`git stash list` empty).

## 2. Scope discrepancy vs. task brief — READ FIRST

The task brief's PROJECT CONTEXT section describes a bag-processing system with
a locked PDF receipt layout engine (`receipt_engine/receipt_3550_attempt6.py`),
a `receipt_adapter.py` mapping layer, label generation, combined "3550"
receipt PDFs, CSV *import*, reprints, and generated-file history.

**None of those exist in this repository.** Verified by full-repo search
(`grep`/`find`, no hits) for `pdf`, `receipt`, `reprint`, `3550`,
`generated-file`, and CSV-import handling; the only CSV/label hits are
Jinja2 form `<label>` tags and CSV/Excel **export** in `reports/report_engine.py`.

What actually exists, per `README.md` and the real source tree, is **Brink's
Nexus**: a cash-in-transit transaction/EOD platform with:

- Multi-catalog data model — one core DB (`core.db`, users/audit) plus four
  isolated per-catalog DBs (VMS, Dayshift, Complete, ESNF), each SQLite by
  default, PostgreSQL supported via `asyncpg` per-database.
- Transactions with a banknote-counter integration (G+D BPS C1 hardware,
  parsed serial/report driver, mock driver for dev) — `backend/services/transaction_service.py`,
  `hardware/`.
- Duplicate-transaction detection (not "bags") — `backend/services/duplicate_detection_service.py`,
  `backend/models/duplicate.py`.
- End-of-day (EOD) closures — the closest analog to "batch completion" —
  `backend/services/eod_service.py`, `backend/services/eod_scheduler.py`.
- Transaction correction (reversal/correction analog) — see
  `alembic_catalog/versions/e3a9c6d1f480_transaction_correction.py`.
- Reports as Excel/CSV export (pandas/openpyxl), not PDF — `reports/report_engine.py`.
- No PDF receipts, no label printing, no CSV import, no reprint flow, no
  generated-file history feature.
- No existing load-test scripts anywhere in the tree.
- No idempotency-key support anywhere in the tree (`grep -ri idempot` = 0 hits).

**Resolution taken:** proceed using the *actual* domain concepts as the
substrate for every required scenario in the brief, mapped 1:1 where a real
analog exists, and explicitly marked "not applicable / not present" where it
doesn't (label generation, PDF receipts, CSV import, reprints, generated-file
history). Constraints #1 and #2 in the brief (do not touch the locked receipt
engine; use `receipt_adapter.py` for mapping) are honored vacuously — those
files do not exist, and this assessment will not invent them. If the user
intended a different repository or a future/parallel branch that contains
the receipt engine, that needs to be pointed at explicitly; this assessment
covers the repository actually present at the branch above.

Mapping table used by all agents below:

| Brief concept | Real equivalent in this repo |
|---|---|
| Bag / duplicate bag | Transaction / duplicate transaction (`duplicate_detection_service.py`) |
| Batch / batch completion | EOD closure (`eod_service.py`, `eod.py`) |
| Combined 3550 receipt PDF, reprints, generated-file history | Not present — out of scope, noted as a gap only if the business intends to add it |
| Label generation | Not present — out of scope |
| CSV import | Not present (export only) — out of scope unless the business wants it added |
| Transaction reference | Transaction identifier/number generation in `transaction_service.py` |
| Correction/reversal | `correct_transaction` flow (`e3a9c6d1f480_transaction_correction.py`, `transaction_service.py`) |

## 3. Baseline automated test suite

Command:
```
python -m venv <venv> && pip install -r requirements.txt
python -m pytest -q
```

Result: **127 passed, 1 skipped, 11 warnings, 19.42s.** Zero failures, zero errors.

Warnings of note (carried into Agent 1/4 findings, not fixed in Phase 0):
- Multiple `SAWarning: garbage collector is trying to clean up non-checked-in
  connection` across `test_duplicates_and_notifications_api.py`,
  `test_eod_and_transfer_api.py`, `test_new_features.py`,
  `test_transactions_api.py`, `test_web_admin_and_transfer.py` — indicates
  some code path acquires a DB session/connection without it being closed
  through the normal context-manager/dependency path. Needs root-causing by
  Agent 1 (transaction boundaries) — this is exactly the kind of leak that
  turns into pool exhaustion under load (relevant to Agent 2 and Agent 4).
- One `PydanticDeprecatedSince20` warning (class-based `Config` in a schema) —
  cosmetic, not in scope per constraint #9.

Full captured output: saved to session scratchpad (`baseline_pytest_output.txt`),
not committed (verbose logs excluded from the repo per commit-strategy rules).

## 4. Environment

| Item | Value |
|---|---|
| OS | Linux 6.18.44-fc-v21, x86_64 (containerized dev/test sandbox, not the target production OS) |
| CPU | 4 vCPU |
| Memory | 15 GiB total, ~14 GiB free at baseline |
| Disk | 252G volume, 30G available to this session |
| Python | 3.11.15 (README/SETUP.md ask for 3.12+; sandbox ships 3.11 — flagged for Agent 4/5, not fixed here) |
| Test client location | In-process ASGI via `httpx.AsyncClient(transport=ASGITransport(...))` — no network hop, not representative of real HTTP/TLS latency; load tests (Agent 2) must use a real HTTP server process instead |
| Database (test suite) | SQLite (aiosqlite), 5 throwaway DBs per test session in a temp dir created by `tests/conftest.py`; never the dev `.db` files or `.env` |
| Database (dev default) | SQLite (aiosqlite) per `backend/core/config.py` defaults |
| Database (target production, per constraint #8) | PostgreSQL — **not yet installed/exercised in this sandbox**; `psql` client binary present, no server; `asyncpg` not installed until a dependency install step. Agent 4 to determine what's needed to stand one up here. |
| App worker count | Not configured anywhere found yet (`run_backend.py`/`uvicorn` — single process by default); to be confirmed by Agent 4/5 |
| Connection-pool settings | None explicit — `backend/core/database.py` creates one `create_async_engine` per database with only `echo`/`connect_args` set, no pool-size/overflow tuning; SQLAlchemy defaults apply. Explicit gap for Agent 4. |
| Dependency versions | Exact pinned versions in `requirements.txt` (fastapi 0.115.0, sqlalchemy 2.0.35, aiosqlite 0.22.1, asyncpg 0.29.0, pydantic 2.9.2, python-jose 3.3.0, passlib[bcrypt] 1.7.4, alembic 1.18.5, apscheduler 3.10.4, jinja2 3.1.6, itsdangerous 2.2.0, pandas 2.2.3, openpyxl 3.1.5, pytest 8.3.3, pytest-asyncio 0.24.0) |

## 5. Credentials / real-data check

- No `.env` file present in the repo (gitignored, not shipped).
- No `*.db` files present in the repo (gitignored).
- `README.md` documents a **default seeded login `admin` / `admin`** for
  local dev, explicitly flagged there as "change these before any production
  use." `database/seed.py` seeds `admin`, `supervisor1`, `cashier1`,
  `cashier2` with demo passwords (`admin`, `super`, `cash1`, `cash2`) — these
  are the `SEEDED_USERS` also used by the test fixtures. No indication these
  are real credentials; they are dev/seed-only. **Confirmed finding for
  Agent 3**: nothing currently prevents these seeded accounts/passwords from
  reaching a production database untouched (constraint from the brief:
  "seeded credentials must not remain valid in production").
- `backend/core/config.py` ships a **hardcoded default `secret_key`**
  (`"change-this-in-production-a-very-long-secret-key-for-jwt"`) with no
  startup check rejecting it — confirmed finding for Agent 3, directly
  matches the brief's required control ("production boot must fail if
  SECRET_KEY is missing, weak, or a known default").
- No real customer data, no production credentials, nothing resembling a
  live system found anywhere in the tree. Safe to proceed.

## 6. Phase 0 actions taken

- Recorded git state (read-only).
- Created an isolated virtualenv (outside the repo, in the session
  scratchpad) and installed pinned dependencies — no repo files changed.
- Ran the existing test suite against its own isolated temp SQLite DBs — no
  repo state, dev DB, or `.env` touched or created.
- No application code, configuration, or dependency versions were modified.
- No destructive commands were run.

Working tree confirmed clean after all of the above (`git status --short`
empty).
