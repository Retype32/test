# Brink's Nexus

A cash-processing management system for armored-carrier / cash-in-transit operations. It's a browser-based application — cashiers, supervisors, and administrators all work through one server-rendered web portal on top of a FastAPI backend.

## Features

- **Multi-catalog data model** — customers, transactions, EOD closures, notifications, and duplicate flags are isolated per processing catalog (VMS / Brink's Dayshift / Brink's Complete / ESNF), while users live in one shared core database.
- **Web portal** (FastAPI + Jinja2) — cashier transaction entry (with banknote-counter integration) plus supervisor/admin views for transactions, EOD, duplicate review, notifications, stats, reports, and user administration.
- **Hardware integration** — pluggable drivers for banknote counters (serial, TCP, and a mock for development), invoked server-side from the transaction-entry page.
- **Reporting** — Excel/CSV report generation.
- **Role-based access** — cashier / supervisor / administrator roles enforced consistently at the API and web-portal layers.
- **Auditability** — core audit log plus duplicate-transaction detection and end-of-day closure tracking.

## Tech Stack

| Layer      | Technology |
|------------|------------|
| Backend    | FastAPI, SQLAlchemy (async), Pydantic, Alembic |
| Database   | SQLite by default (aiosqlite), PostgreSQL supported (asyncpg) |
| Web portal | Jinja2, signed session cookies |
| Auth       | JWT (JSON API), session cookies (web) |
| Reports    | pandas, openpyxl |
| Testing    | pytest, pytest-asyncio |

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows — use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

python run_backend.py         # starts the app at http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/web/login` in a browser. Default login: `admin` / `admin` (see [SETUP.md](SETUP.md) for the full credentials table — **change these before any production use**).

Full setup instructions, environment configuration, project structure, and architecture notes live in **[SETUP.md](SETUP.md)**.

## Running Tests

```bash
pytest
```

Test configuration lives in [pytest.ini](pytest.ini); coverage includes auth, catalogs, transactions, EOD/transfer, duplicates/notifications, stats, and the web portal.

## Project Structure

See [SETUP.md](SETUP.md#project-structure) for the annotated directory layout and architecture notes (Clean Architecture layering, multi-catalog design, migrations).
