# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Setup:
```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
python -m database.seed       # first run only; auto-runs on server startup if core.db has no users
```

Run:
```bash
python run_backend.py         # http://127.0.0.1:8000 ; API docs at /docs, web portal at /web/login
```
`run_backend.py` currently passes `reload=True` to uvicorn — be aware a file-change restart will drop any live serial connection to the counter mid-transaction if hardware is attached.

Tests:
```bash
pytest                                    # full suite (config: pytest.ini)
pytest tests/test_c1_driver.py            # one file
pytest tests/test_c1_driver.py::test_name # one test
```
No lint/format/type-check tooling is configured in this repo.

Migrations (Alembic; `init_databases()` auto-creates tables and stamps head on every startup, so manual runs are only needed when evolving the schema):
```bash
alembic upgrade head                                                                # core (users) database
alembic -c alembic_catalog.ini -x catalog=<vms|dayshift|complete|esnf> upgrade head  # one catalog database
```

Hardware diagnostics (most work without a physical machine attached):
```bash
python -m hardware.capture --list-ports          # which COM ports exist
python -m hardware.capture --doctor --port COM3  # full preflight against a real machine
python -m hardware.capture --port-status         # what's holding a busy port
python -m hardware.simulate --check              # exercise the parser against 6 simulated print layouts
```

## Architecture

**Multi-catalog data model.** `User` lives in one shared core database (`core.db`). Everything else — customers, transactions, EOD closures, notifications, duplicate flags, audit log — lives in one fully separate SQLite database per **catalog** (`CatalogCode` in `backend/core/catalogs.py`: `vms` / `dayshift` / `complete` / `esnf`). A request's `X-Catalog` header (JSON API, `backend/api/deps.py`) or session-stored catalog (web portal, `web/deps.py`) selects which database engine/sessionmaker it hits (`backend/core/database.py`). Nothing crosses catalog boundaries; a catalog is chosen once per web session via `/web/catalog/select` and can be switched from the sidebar without logging out.

**Clean Architecture layering**, applied consistently to both the JSON API (`backend/api/routes/`) and the web portal (`web/routes/`): routes → `backend/services/` (business logic) → `backend/repositories/` (data access) → `backend/models/` (SQLAlchemy ORM). `backend/schemas/` holds Pydantic request/response models for the JSON API; the web portal renders Jinja2 templates (`web/templates/`) directly from service/model objects instead of going through schemas.

**Hardware integration** (`hardware/`) is invoked only from `web/routes/transaction_entry_web.py`. `CashCounter` (`hardware/base.py`) is the driver interface — `connect()`, `disconnect()`, `is_connected()`, `wait_for_count_result()`. Two implementations select via `COUNTER_MODE` in `.env`: `c1_report` (`GDC1ReportCounter` — reads the G+D BPS C1's serial printer port; it never writes to the port, enforced by a test) and `mock` (`MockCounter`, no hardware required). The C1 has no command API: it only emits a batch report when an operator ends a batch on its own panel, so the driver frames a report by its `ESC i` terminator or an idle-time fallback, then `hardware/report_parser.py` parses it against a per-machine `hardware/profiles/*.json` device profile (denomination label matching, comms settings, totals cross-validation against the machine's own printed totals).

`hardware/__init__.py` holds one shared counter connection for the whole process lifetime rather than reconnecting per transaction (a serial port has exactly one owner). It reconnects on demand if the shared connection was never established or has gone stale (`is_connected()` fails, or a `SerialException` occurs mid-read), instead of requiring an app restart. `ping_shared_counter()` is a non-blocking liveness check called at the start of a transaction's cash step and after every batch; `wait_for_shared_count()` is the blocking read that actually captures a batch. The wizard's cash-count screen (`web/templates/wizard_cash.html`) continuously re-polls `POST /transactions/new/count` for as long as it's open, adding every machine batch on top of a running per-transaction pool alongside manual entries — nothing is ever overwritten wholesale.

**Transactions are append-only.** A correction (`TransactionService.correct_transaction`) never edits or deletes the original row; it creates a new transaction pointing back at it via `original_transaction_id` and flags the original `is_superseded`, so both remain in the record. `EODService` locks a `business_date` per catalog against new transactions once closed (manually via the EOD page, or automatically at midnight via `eod_scheduler.py`'s APScheduler job); a closed day can only be reopened by an administrator with a reason, and corrections deliberately bypass the closed-day gate since they fix an existing record rather than add new activity.

**Auth**: JWT bearer tokens for the JSON API (`backend/core/security.py`), signed session cookies for the web portal — both hash passwords with bcrypt. Roles are cashier / supervisor / administrator, enforced via FastAPI dependencies (`require_role` in `backend/api/deps.py`, `require_web_role` in `web/deps.py`) at both layers.

## Known gaps (pre-production pilot — not yet the system of record for real money)

- `run_backend.py` runs uvicorn with `reload=True`, a dev setting that will drop the live serial connection to the counter on any file-change restart mid-transaction. Should be off outside active development.
- `database/seed.py` seeds `admin/admin`, `supervisor1/super`, `cashier1/cash1`, `cashier2/cash2` **and resets passwords back to these defaults on every re-seed** — SETUP.md documents re-seeding as a normal step, so a routine re-seed silently reverts any password change. `backend/core/config.py`'s `secret_key` (signs sessions/JWTs) also ships a literal hardcoded default.
- No `.env.example` exists in the repo (`.env` is gitignored); SETUP.md's sample `COUNTER_COM_PORT=COM3` doesn't match the device profile's `COM1` default, which reproduces a "machine not connected" symptom on a fresh install.
- `backend/services/backup_service.py` (correctly uses SQLite's online backup API against the live databases) only runs on manual admin click, writing to `backups/` on the same disk as the live databases — not yet scheduled or copied off-machine.
- Machine connection status (`ping_shared_counter()`) is only surfaced on the wizard's cash step, not visible elsewhere (e.g. the dashboard).
- A persisted transaction doesn't record whether its count came from the machine or was hand-typed. `CountResult.raw_response` carries the raw machine report but it's currently discarded rather than persisted.
