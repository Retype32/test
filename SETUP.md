# Brink's Nexus — Setup Guide

## Prerequisites

- Python 3.12+
- Git

SQLite is the default database (zero setup required). PostgreSQL is supported per-database via `asyncpg` if you'd rather point any of the databases below at a real server — just change the matching `DATABASE_URL_*` value in `.env`.

---

## 1. Install Python Dependencies

```bash
cd BrinksNexus
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## 2. Configure Environment

`.env` already ships with working local defaults. Edit it if you need to change database locations, the JWT secret, or hardware counter settings:

```env
DATABASE_URL_CORE=sqlite+aiosqlite:///./core.db
DATABASE_URL_VMS=sqlite+aiosqlite:///./catalog_vms.db
DATABASE_URL_DAYSHIFT=sqlite+aiosqlite:///./catalog_dayshift.db
DATABASE_URL_COMPLETE=sqlite+aiosqlite:///./catalog_complete.db
DATABASE_URL_ESNF=sqlite+aiosqlite:///./catalog_esnf.db
SECRET_KEY=brinks-nexus-super-secret-key-change-in-production-2026
SESSION_COOKIE_SECURE=false
```

**`SESSION_COOKIE_SECURE`** guards the web portal's login cookie — leave it `false` for local/LAN HTTP use, set it `true` once the portal is served over HTTPS.

---

## 3. Start the Backend Server

```bash
python run_backend.py
```

The first time this runs it automatically creates all five databases (`core.db` for users, plus one isolated database per processing catalog: `catalog_vms.db`, `catalog_dayshift.db`, `catalog_complete.db`, `catalog_esnf.db`) and seeds them with demo data if they're empty — no manual seed step required for a fresh install.

- API: `http://127.0.0.1:8000`
- Interactive docs: `http://127.0.0.1:8000/docs`
- Web portal (supervisors/admins only): `http://127.0.0.1:8000/web/login`

To re-seed manually at any point (e.g. after wiping a `.db` file):

```bash
python -m database.seed
```

---

## 4. Open the Web Portal

With the backend running, open `http://127.0.0.1:8000/web/login` in a browser. `start.bat` (Windows) starts the backend for you and prints that URL.

After logging in, you'll be asked to pick a **Processing Catalog** (VMS / Brink's Dayshift / Brink's Complete / ESNF) — each is a fully separate dataset (its own customers, transactions, reports). You can switch catalogs at any time from the sidebar without logging out. Cashiers land on **New Transaction**; supervisors and administrators land on the **Dashboard** and additionally get Transaction Viewer, Reports, Statistics, EOD, and Duplicates (administrators also get Staff management).

---

## Default Credentials

| Username    | Password | Role          |
|-------------|----------|---------------|
| admin       | admin    | Administrator |
| supervisor1 | super    | Supervisor    |
| cashier1    | cash1    | Cashier       |
| cashier2    | cash2    | Cashier       |

**Change all passwords before any production use.**

---

## Project Structure

```text
BrinksNexus/
├── backend/
│   ├── api/routes/        — FastAPI JSON route handlers (auth, transactions, eod, notifications, duplicates, stats, ...)
│   ├── core/              — Config, catalog registry, core/catalog DB engines, security
│   ├── models/             — SQLAlchemy ORM models (User is core-only; everything else is per-catalog)
│   ├── repositories/       — Data access layer
│   ├── schemas/            — Pydantic request/response models
│   ├── services/           — Business logic layer (EOD locking, duplicate detection, stats aggregation, ...)
│   └── main.py             — FastAPI app factory; wires JSON API + web portal + session middleware
├── alembic/                — Migrations for the core (users) database
├── alembic_catalog/        — Migrations for the shared catalog schema (run once per catalog, see below)
├── database/
│   └── seed.py             — Sample data seeder (core users + all 4 catalogs)
├── web/                     — Server-rendered web portal (Jinja2) — the only front end
│   ├── routes/              — One module per page area, mirrors backend/api/routes
│   │                          (transaction_entry_web.py is the cashier New Transaction flow)
│   ├── templates/           — Jinja2 templates
│   └── static/css/theme.css — Portal color palette/stylesheet
├── reports/
│   └── report_engine.py    — Excel + CSV export engine
├── hardware/                — Banknote counter drivers (serial/TCP/mock), invoked from
│                               web/routes/transaction_entry_web.py
├── run_backend.py           — Backend + web portal server launcher
├── start.bat                — Windows launcher (starts the backend, prints the portal URL)
├── requirements.txt
└── .env
```

---

## Architecture Notes

- **Multi-catalog data model**: `User` lives in a shared core database (`core.db`); everything else (customers, transactions, EOD closures, notifications, duplicate flags) lives in one fully separate database per catalog. A request's `X-Catalog` header (JSON API) or session-stored catalog (web) selects which one it hits.
- **Clean Architecture**: UI → Services → Repositories → Models, consistently across the JSON API and web portal.
- **Authentication**: JWT Bearer tokens for the JSON API; signed session cookies for the web portal. Both hash passwords with bcrypt.
- **Async backend**: FastAPI + SQLAlchemy async (aiosqlite by default, asyncpg if you point a `DATABASE_URL_*` at Postgres).
- **Role-based access**: cashier / supervisor / administrator, enforced at the API layer and mirrored in the web portal's navigation and route guards. Cashiers see New Transaction only; supervisors additionally see Dashboard/Transaction Viewer/Reports/Statistics/EOD/Duplicates; administrators also see Staff management.
- **Migrations**: schema changes are tracked with Alembic. The core database uses `alembic.ini`; the catalog schema (shared by all 4 catalog databases) uses `alembic_catalog.ini` with a `-x catalog=<vms|dayshift|complete|esnf>` flag to target a specific database, e.g.:

  ```bash
  alembic -c alembic_catalog.ini -x catalog=vms upgrade head
  ```

  `run_backend.py`'s startup also auto-creates any missing tables and stamps each database to the latest revision, so a normal `pip install && python run_backend.py` never requires running Alembic by hand — it's there for when you need to evolve the schema without losing existing data.
