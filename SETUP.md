# Brink's Nexus — Setup Guide

## Prerequisites

- Python 3.13+
- PostgreSQL 15+
- Git

---

## 1. Create PostgreSQL Database

Open psql or pgAdmin and run:

```sql
CREATE USER nexus WITH PASSWORD 'nexus';
CREATE DATABASE brinksdb OWNER nexus;
GRANT ALL PRIVILEGES ON DATABASE brinksdb TO nexus;
```

---

## 2. Install Python Dependencies

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

## 3. Configure Environment

Edit `.env` if you need to change database credentials or the secret key:

```env
DATABASE_URL=postgresql+asyncpg://nexus:nexus@localhost:5432/brinksdb
SECRET_KEY=brinks-nexus-super-secret-key-change-in-production-2026
```

---

## 4. Start the Backend Server

```bash
python run_backend.py
```

The API will be available at: http://127.0.0.1:8000
Interactive docs: http://127.0.0.1:8000/docs

---

## 5. Seed the Database

In a second terminal (with venv active):

```bash
python -m database.seed
```

This creates:
- **Users**: admin / admin123, supervisor1 / super123, cashier1 / cash123, cashier2 / cash123
- **Customers**: Tesco, AIB, Dunnes Stores, SuperValu (with locations)
- **Sample transactions**

---

## 6. Launch the Desktop Application

```bash
python main.py
```

Log in with any of the seeded users.

---

## Default Credentials

| Username    | Password  | Role          |
|-------------|-----------|---------------|
| admin       | admin123  | Administrator |
| supervisor1 | super123  | Supervisor    |
| cashier1    | cash123   | Cashier       |
| cashier2    | cash123   | Cashier       |

**Change all passwords before any production use.**

---

## Project Structure

```
BrinksNexus/
├── backend/
│   ├── api/routes/       — FastAPI route handlers
│   ├── core/             — Config, DB engine, security
│   ├── models/           — SQLAlchemy ORM models
│   ├── repositories/     — Data access layer
│   ├── schemas/          — Pydantic request/response models
│   ├── services/         — Business logic layer
│   └── main.py           — FastAPI app factory
├── database/
│   └── seed.py           — Sample data seeder
├── frontend/
│   ├── services/
│   │   └── api_client.py — HTTP client for backend API
│   └── ui/
│       ├── dialogs/      — Modal dialogs
│       ├── pages/        — Full-page views
│       ├── main_window.py — Main app window + sidebar
│       └── theme.py      — Colors, fonts, stylesheet
├── reports/
│   └── report_engine.py  — Excel + CSV export engine
├── main.py               — Desktop app entry point
├── run_backend.py        — Backend server launcher
├── requirements.txt
└── .env
```

---

## Architecture Notes

- **Clean Architecture**: UI → Services → Repositories → Models
- **Authentication**: JWT Bearer tokens, bcrypt password hashing
- **Async backend**: FastAPI + asyncpg + SQLAlchemy async
- **Sync frontend calls**: httpx synchronous client (PySide6 main thread)
- **Role-based access**: cashier / supervisor / administrator enforced at API level

---

## Running Both at Once (Windows)

Create `start.bat`:

```bat
start cmd /k "python run_backend.py"
timeout /t 3
python main.py
```
