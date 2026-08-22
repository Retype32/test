"""Shared helpers for the loadtest scripts in this directory.

These scripts talk to a *running* server over real HTTP (unlike the test
suite's in-process ASGITransport clients) because the whole point is to
measure what a real uvicorn process + real SQLite file does under
concurrency -- connection handling, event-loop scheduling, and file-level
locking don't exist in the in-process test transport.
"""
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field

import httpx

# Matches database/seed.py -- the only accounts a fresh install has.
SEEDED_USERS = [
    ("admin", "admin"),
    ("supervisor1", "super"),
    ("cashier1", "cash1"),
    ("cashier2", "cash2"),
]

# (customer_id, location_id) pairs seeded into every catalog.
CUSTOMER_LOCATIONS = [
    ("C001", "L001"), ("C001", "L002"), ("C001", "L003"), ("C001", "L004"),
    ("C002", "L010"), ("C002", "L011"), ("C002", "L012"),
    ("C003", "L020"), ("C003", "L021"),
    ("C004", "L030"), ("C004", "L031"), ("C004", "L032"),
]

DENOM_VALUES = {"€500": 500, "€200": 200, "€100": 100, "€50": 50, "€20": 20, "€10": 10, "€5": 5, "Coins": 1}

DEFAULT_CATALOG = "vms"


async def login(client: httpx.AsyncClient, base_url: str, username: str, password: str) -> str:
    r = await client.post(f"{base_url}/api/v1/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


async def login_all_seeded(client: httpx.AsyncClient, base_url: str) -> list[str]:
    tokens = []
    for username, password in SEEDED_USERS:
        tokens.append(await login(client, base_url, username, password))
    return tokens


def random_transaction_payload(tag: str = "LOADTEST") -> dict:
    customer_id, location_id = random.choice(CUSTOMER_LOCATIONS)
    denom = random.choice(list(DENOM_VALUES))
    count = random.randint(1, 25)
    value = DENOM_VALUES[denom] * count
    return {
        "customer_id": customer_id,
        "location_id": location_id,
        "bag_number": f"{tag}-{uuid.uuid4().hex[:16]}",
        "total_value": str(value),
        "denominations": [{"denomination": denom, "count": count, "value": str(value)}],
    }


@dataclass
class RequestResult:
    ok: bool
    status_code: int | None
    latency: float
    error: str | None = None


@dataclass
class ResultSet:
    label: str
    results: list[RequestResult] = field(default_factory=list)
    wall_time: float = 0.0

    def add(self, r: RequestResult):
        self.results.append(r)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def successes(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failures(self) -> int:
        return self.total - self.successes

    @property
    def error_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0

    @property
    def throughput(self) -> float:
        return self.total / self.wall_time if self.wall_time else 0.0

    def latency_percentiles(self) -> dict:
        lat = sorted(r.latency for r in self.results if r.ok)
        if not lat:
            return {"p50": None, "p95": None, "p99": None, "max": None, "avg": None}
        def pct(p):
            idx = min(len(lat) - 1, int(len(lat) * p))
            return lat[idx]
        return {
            "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99),
            "max": lat[-1], "avg": statistics.mean(lat),
        }

    def error_breakdown(self) -> dict:
        buckets: dict[str, int] = {}
        for r in self.results:
            if not r.ok:
                key = r.error or f"HTTP {r.status_code}"
                buckets[key] = buckets.get(key, 0) + 1
        return buckets

    def locked_count(self) -> int:
        return sum(
            1 for r in self.results
            if not r.ok and r.error and "locked" in r.error.lower()
        )

    def print_summary(self):
        p = self.latency_percentiles()
        print(f"\n=== {self.label} ===")
        print(f"  requests:     {self.total}")
        print(f"  wall time:    {self.wall_time:.2f}s")
        print(f"  throughput:   {self.throughput:.1f} req/s")
        print(f"  success:      {self.successes} ({100 * (1 - self.error_rate):.1f}%)")
        print(f"  failures:     {self.failures} ({100 * self.error_rate:.1f}%)")
        if p["avg"] is not None:
            print(f"  latency avg/p50/p95/p99/max (ms): "
                  f"{p['avg']*1000:.0f} / {p['p50']*1000:.0f} / {p['p95']*1000:.0f} / "
                  f"{p['p99']*1000:.0f} / {p['max']*1000:.0f}")
        if self.failures:
            print(f"  'database is locked' errors: {self.locked_count()}")
            print("  error breakdown:")
            for key, count in sorted(self.error_breakdown().items(), key=lambda kv: -kv[1]):
                print(f"    {count:5d}  {key}")


async def timed_request(coro) -> RequestResult:
    start = time.perf_counter()
    try:
        resp = await coro
        latency = time.perf_counter() - start
        if resp.status_code < 400:
            return RequestResult(True, resp.status_code, latency)
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))[:120]
        except Exception:
            detail = resp.text[:120]
        return RequestResult(False, resp.status_code, latency, error=f"HTTP {resp.status_code}: {detail}")
    except Exception as e:
        latency = time.perf_counter() - start
        return RequestResult(False, None, latency, error=f"{type(e).__name__}: {e}")
