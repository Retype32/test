"""Test-data / environment setup helpers (plan section 11): seeding extra
synthetic cashier/supervisor accounts beyond the 2+2 the app ships with by
default, so higher-VU tiers get one real account per virtual user rather
than every VU sharing cashier1/cashier2 (which the plan flags as
"possible but not representative of real per-cashier attribution in the
audit trail")."""
from __future__ import annotations

import httpx

from . import config


async def _admin_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": config.SEEDED_USERS["admin"]["password"]},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def ensure_cashier_accounts(base_url: str, needed: int) -> list[tuple[str, str]]:
    """Returns `needed` (username, password) cashier credential pairs,
    reusing the 2 seeded cashiers first and creating
    loadtest_cashier_{N} accounts (role=cashier) for the rest via the
    admin user-management API. Idempotent: re-running with the same or a
    smaller `needed` does not recreate existing accounts."""
    seeded_cashiers = [
        (u, info["password"]) for u, info in config.SEEDED_USERS.items() if info["role"] == "cashier"
    ]
    if needed <= len(seeded_cashiers):
        return seeded_cashiers[:needed]

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        token = await _admin_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        existing = await client.get("/api/v1/auth/users", headers=headers)
        existing.raise_for_status()
        existing_usernames = {u["username"] for u in existing.json()}

        accounts = list(seeded_cashiers)
        extra_needed = needed - len(seeded_cashiers)
        for i in range(extra_needed):
            username = f"{config.SYNTHETIC_CASHIER_PREFIX}{i:04d}"
            if username not in existing_usernames:
                resp = await client.post(
                    "/api/v1/auth/users", headers=headers,
                    json={"username": username, "password": config.SYNTHETIC_PASSWORD, "role": "cashier"},
                )
                if resp.status_code not in (201, 400):  # 400 = "already exists" race, harmless
                    resp.raise_for_status()
            accounts.append((username, config.SYNTHETIC_PASSWORD))
        return accounts


async def ensure_supervisor_accounts(base_url: str, needed: int) -> list[tuple[str, str]]:
    seeded = [
        (u, info["password"]) for u, info in config.SEEDED_USERS.items()
        if info["role"] in ("supervisor", "administrator")
    ]
    if needed <= len(seeded):
        return seeded[:needed]

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        token = await _admin_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        existing = await client.get("/api/v1/auth/users", headers=headers)
        existing.raise_for_status()
        existing_usernames = {u["username"] for u in existing.json()}

        accounts = list(seeded)
        extra_needed = needed - len(seeded)
        for i in range(extra_needed):
            username = f"{config.SYNTHETIC_SUPERVISOR_PREFIX}{i:04d}"
            if username not in existing_usernames:
                resp = await client.post(
                    "/api/v1/auth/users", headers=headers,
                    json={"username": username, "password": config.SYNTHETIC_PASSWORD, "role": "supervisor"},
                )
                if resp.status_code not in (201, 400):
                    resp.raise_for_status()
            accounts.append((username, config.SYNTHETIC_PASSWORD))
        return accounts


async def wait_for_server(base_url: str, timeout_s: float = 30.0) -> bool:
    """Polls until the server responds (any status), or gives up. Used
    before a run starts so a not-yet-ready uvicorn doesn't get counted as
    a wave of unexpected_failure connection-refused requests."""
    import asyncio
    import time

    deadline = time.time() + timeout_s
    async with httpx.AsyncClient(base_url=base_url, timeout=3.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get("/web/login")
                if resp.status_code < 500:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False
