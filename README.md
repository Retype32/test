# Brink's Nexus

A cash-processing management system for armored-carrier / cash-in-transit operations. It combines a desktop application (for cashiers processing deposits) with a server-rendered web portal (for supervisors and administrators) on top of a shared FastAPI backend.

## Features

- **Multi-catalog data model** — customers, transactions, EOD closures, notifications, and duplicate flags are isolated per processing catalog (VMS / Brink's Dayshift / Brink's Complete / ESNF), while users live in one shared core database.
- **Desktop app** (PySide6) — cashier-facing UI for transaction entry, banknote-counter integration, reports, and stats.
- **Web portal** (FastAPI + Jinja2) — supervisor/admin-only views for transactions, EOD, duplicate review, notifications, stats, and user administration.
- **Hardware integration** — pluggable drivers for banknote counters (serial, TCP, and a mock for development).
- **Reporting** — Excel/CSV report generation shared between the desktop app and web portal.
- **Role-based access** — cashier / supervisor / administrator roles enforced consistently at the API layer.
- **Auditability** — core audit log plus duplicate-transaction detection and end-of-day closure tracking.

## Tech Stack

| Layer      | Technology |
|------------|------------|
| Backend    | FastAPI, SQLAlchemy (async), Pydantic, Alembic |
| Database   | SQLite by default (aiosqlite), PostgreSQL supported (asyncpg) |
| Web portal | Jinja2, signed session cookies |
| Desktop    | PySide6 |
| Auth       | JWT (API/desktop), session cookies (web) |
| Reports    | pandas, openpyxl |
| Testing    | pytest, pytest-asyncio |

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows — use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

python run_backend.py         # starts the API + web portal at http://127.0.0.1:8000
python main.py                # in a second terminal, starts the desktop app
```

Default login: `admin` / `admin` (see [SETUP.md](SETUP.md) for the full credentials table — **change these before any production use**).

Full setup instructions, environment configuration, project structure, and architecture notes live in **[SETUP.md](SETUP.md)**.

## Running Tests

```bash
pytest
```

Test configuration lives in [pytest.ini](pytest.ini); coverage includes auth, catalogs, transactions, EOD/transfer, duplicates/notifications, stats, and the web portal.

## Project Structure

See [SETUP.md](SETUP.md#project-structure) for the annotated directory layout and architecture notes (Clean Architecture layering, multi-catalog design, migrations).
