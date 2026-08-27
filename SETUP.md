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

COUNTER_COM_PORT=
COUNTER_BAUD_RATE=115200
```

**`SESSION_COOKIE_SECURE`** guards the web portal's login cookie — leave it `false` for local/LAN HTTP use, set it `true` once the portal is served over HTTPS.

**`COUNTER_COM_PORT`** connects a real G+D BPS C1. Leave it blank to run without hardware — the wizard's machine-read step simply fails immediately with a clear message and cashiers enter counts manually. See [Connecting a BPS C1](#connecting-a-bps-c1) below.

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

## Connecting a BPS C1

The G+D BPS C1 has no host command API. It drives a serial receipt printer, and
integration works by connecting the PC to that printer port and parsing the
batch report the machine prints. Nexus never sends the machine a command — it
only listens. This is the same approach the ISA device plugin uses, and the
same approach the standalone `C1 Check.py` capture/evidence tool at the
project root proved against a real machine — `hardware/c1_engine.py` carries
that exact connection and parsing logic into the app itself.

There is deliberately one supported layout: EUR denominations printed as bare
face values (`50 X 10 = 500.00`, etc.), 115200 8N1 no handshake. Adding another
currency or machine means editing `hardware/c1_engine.py`, not swapping a
config file.

**1. Configure the machine.** On the C1's operator panel, route report/printer
output to a serial interface at **115200 8N1, no handshake** (the settings ISA
uses, taken from its plugin config).

Note that `COUNTER_COM_PORT` and the `port` value in ISA's plugin config are
both **PC-side** Windows port names. Neither says anything about which physical
socket on the C1 is in use, or how many serial interfaces the machine has —
consult the C1's own interface menu or G+D documentation for that.

**2. Wire it up.** Two options, and USB is the easier one if the machine
supports it:

- **USB** — a standard A-to-B cable from the machine's USB device socket to the
  PC. Industrial equipment usually presents this as a *virtual COM port* (USB
  CDC, or an internal FTDI/Prolific bridge). If it does, Windows assigns it a
  COM number and it is read exactly like RS232 — no adapter, no null-modem
  cable, and no code change, because a virtual COM port is a COM port.
- **RS232** — a null-modem cable from the machine's serial printer port. If the
  PC has no RS232 socket, add a USB-to-serial adapter (FTDI or Prolific).

Either way, confirm Windows sees a port:

```bash
python "C1 Check.py" --list-ports
```

This identifies USB-attached ports and names the bridge chip. If nothing
appears while the machine is plugged in over USB, it is not presenting a
virtual COM port — check Device Manager for an unrecognised device under
"Other devices", which means a vendor USB driver is missing. Stop and fix this
before going further; every later step depends on it.

**3. Run the checker as a preflight.** `C1 Check.py` is the same read-only
tool that proved this connection against a real machine, and it is the
sanctioned way to verify a new site before switching Nexus on — it listens on
the port, frames and parses whatever the C1 prints, and writes the evidence to
`c1_nexus_data/` (raw bytes, cleaned text, parsed JSON) so a failure is
diagnosable instead of a guess:

```bash
python "C1 Check.py" --port COM3
```

Run a batch on the machine during its listen window and watch the printed
report summary. If it comes back `READY` with the expected denominations and
total, the site is good — skip to step 4. If it comes back `INCOMPLETE`,
the summary names exactly which check failed and `c1_nexus_data/incomplete/`
holds the evidence to look at.

**4. Switch the driver on** in `.env`:

```env
COUNTER_COM_PORT=COM3
COUNTER_BAUD_RATE=115200
```

Validation is always strict — a report whose printed totals disagree with the
sum of its own denomination lines is always rejected, not accepted with a
warning. A partially read report is the main failure mode of printer-port
integration, and silently accepting one would book a short count as a real
one. Nexus also refuses to book the exact same physical report twice (a
receipt reprint, a retried request, a repeated poll landing on the same
batch) — repeats are rejected as duplicates rather than double-counted.

### The C1's USB port

The C1's USB interface is not a virtual COM port. G+D drive it with their MAUSB
driver, which is a rebadged **libusb-win32**: `mausb.sys` identifies itself as
"LibUSB-Win32 - Kernel Driver" and creates `\Device\libusb0`, and `mausb.dll`
exports the standard libusb-0.1 API. `MAUSB.inf` binds it to `USB\VID_10c4&PID_ea63`
under class `USB`, not class `Ports` — so no COM port appears and neither the
serial driver here nor ISA's plugin (which is `System.IO.Ports` only) can use it.
This path is not wired into Nexus; use the RS232/USB-CDC serial path above.

### Testing without a machine

Leave `COUNTER_COM_PORT` blank and the wizard's machine-read step fails
immediately with a clear message — cashiers enter counts manually, which
always works regardless of hardware. `tests/test_c1_engine.py` and
`tests/test_c1_counter_driver.py` exercise the parsing and framing logic
directly with no port involved. To drive the real driver end to end without a
physical machine, install a virtual null-modem pair (com0com on Windows),
point `COUNTER_COM_PORT` at one end, and replay a captured report (see
`c1_nexus_data/raw/` after a real capture, or the sample report in
`tests/test_c1_engine.py`) into the other.

### Running on a PC that already has ISA

**Nexus will not open the port while ISA is running.** A serial port has exactly
one owner, and ISA's BPS C1 plugin opens the machine's printer port when the
plugin loads and holds it until the plugin unloads. Nexus will fail at
`connect()` with "already in use".

ISA is browser-based, so there is no window to close. The browser is only its
user interface; the component holding the port is an ISA service or background
process on the PC the machine is cabled to.

| Option | Both keep working? | Effort |
|---|---|---|
| Close ISA while using Nexus | No — one at a time | admin sign-off |
| Second report output on another C1 interface, if the firmware allows one | Yes | machine config |
| Passive Y-cable tap on the C1's transmit line | Yes | a cable |
| Nexus on a separate PC, own cable | Yes | a PC and a cable |

The passive tap works only because this driver is listen-only — it never
transmits a byte, so it cannot corrupt ISA's data or disturb the machine. That
is enforced by a test (`test_driver_never_writes_to_the_port`). Get sign-off
from whoever owns the ISA installation before closing it or touching production
cabling.

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
├── hardware/                — G+D BPS C1 serial connection, invoked from
│   │                           web/routes/transaction_entry_web.py
│   ├── __init__.py          — C1Counter driver + process-wide shared connection
│   └── c1_engine.py         — ESC/P cleanup, report parsing/validation, stream framing, duplicate detection
├── C1 Check.py              — Standalone read-only capture/preflight tool (see "Connecting a BPS C1")
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
