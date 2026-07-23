"""Shared pytest fixtures for the Brink's Nexus backend/web test suite.

Everything runs against a throwaway set of SQLite databases in a temp
directory (never the real dev .db files or .env). The env vars below MUST be
set before `backend.core.config` is imported anywhere, since Settings() is a
module-level singleton read once at import time -- that's why this happens
at conftest module scope instead of inside a fixture function. pytest always
imports a directory's conftest.py before collecting test modules in it, so
this ordering is safe as long as no test module imports backend/web at its
own module top level (they should only do so inside fixtures/tests).
"""
import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="brinksnexus_test_")

os.environ["DATABASE_URL_CORE"] = f"sqlite+aiosqlite:///{_TMP_DIR}/core.db"
os.environ["DATABASE_URL_VMS"] = f"sqlite+aiosqlite:///{_TMP_DIR}/catalog_vms.db"
os.environ["DATABASE_URL_DAYSHIFT"] = f"sqlite+aiosqlite:///{_TMP_DIR}/catalog_dayshift.db"
os.environ["DATABASE_URL_COMPLETE"] = f"sqlite+aiosqlite:///{_TMP_DIR}/catalog_complete.db"
os.environ["DATABASE_URL_ESNF"] = f"sqlite+aiosqlite:///{_TMP_DIR}/catalog_esnf.db"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["DEBUG"] = "false"

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


SEEDED_USERS = {
    "admin": "admin",
    "supervisor1": "super",
    "cashier1": "cash1",
    "cashier2": "cash2",
}


@pytest_asyncio.fixture(scope="session")
async def app():
    """Builds the FastAPI app against the temp databases and seeds demo data
    once for the whole test session. Bypasses the app's own lifespan (which
    only calls init_databases()/auto-seed) since we do the equivalent here
    directly -- httpx's ASGITransport doesn't run lifespan events itself."""
    from database.seed import seed
    await seed()
    from backend.main import app as fastapi_app
    return fastapi_app


@pytest_asyncio.fixture(scope="session")
async def tokens(app):
    """Bearer tokens for each seeded user, keyed by username."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        result = {}
        for username, password in SEEDED_USERS.items():
            r = await client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert r.status_code == 200, f"seed login failed for {username}: {r.text}"
            result[username] = r.json()["access_token"]
        return result


@pytest_asyncio.fixture
async def api_client(app):
    """Fresh httpx client per test (no shared cookies), talking to the
    shared in-process app/database over ASGI -- no real server needed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def web_client(app):
    """Same as api_client but semantically for the cookie-session web portal
    -- kept separate so each web test starts from a clean, unauthenticated
    cookie jar regardless of what other tests did."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def auth_headers(token: str, catalog: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if catalog:
        headers["X-Catalog"] = catalog
    return headers
