"""Virtual-user session plumbing and the two concurrency drivers the
profiles use: run-for-a-fixed-duration (Profile A and friends) and
run-a-fixed-total-attempt-count (Profile B).

Per the plan's tool choice (section 8): no distributed master/worker
architecture, no locust -- a semaphore/task-pool of hand-rolled coroutines
is enough at the tens-to-low-hundreds VU scale this app is scoped for.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from .httpclient import TimedClient, new_api_client, new_web_client
from .metrics import MetricsCollector


@dataclass
class Session:
    """One virtual user's state: its own cookie-jar web client (a real
    browser tab) and, lazily, its own bearer-token API client. Never
    shared across VUs -- each VU is an independent actor, exactly like a
    real cashier/supervisor would be."""

    base_url: str
    collector: MetricsCollector
    vu_id: str
    username: str
    password: str
    catalog: str
    think_min: float = 0.5
    think_max: float = 2.0
    rng: random.Random = field(default_factory=random.Random)

    web_client: Optional[httpx.AsyncClient] = None
    web_tc: Optional[TimedClient] = None
    api_client: Optional[httpx.AsyncClient] = None
    api_tc: Optional[TimedClient] = None
    api_token: Optional[str] = None

    def __post_init__(self):
        self.web_client = new_web_client(self.base_url)
        self.web_tc = TimedClient(self.web_client, self.collector, self.vu_id)

    def api(self) -> TimedClient:
        if self.api_client is None:
            self.api_client = new_api_client(self.base_url)
            self.api_tc = TimedClient(self.api_client, self.collector, self.vu_id)
        return self.api_tc

    async def think(self, scale: float = 1.0) -> None:
        lo, hi = self.think_min * scale, self.think_max * scale
        if hi <= 0:
            return
        await asyncio.sleep(self.rng.uniform(lo, hi))

    async def ensure_api_token(self) -> Optional[str]:
        if self.api_token:
            return self.api_token
        resp = await self.api().call(
            "POST", "/api/v1/auth/login",
            json={"username": self.username, "password": self.password},
            success_statuses=(200,),
        )
        if resp is not None and resp.status_code == 200:
            self.api_token = resp.json()["access_token"]
        return self.api_token

    def api_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_token}", "X-Catalog": self.catalog}

    async def aclose(self) -> None:
        if self.web_client is not None:
            await self.web_client.aclose()
        if self.api_client is not None:
            await self.api_client.aclose()


IterationFn = Callable[[Session], Awaitable[None]]


async def _vu_duration_loop(
    vu_index: int,
    make_session: Callable[[int], Session],
    iteration_fn: IterationFn,
    deadline: float,
    errors: list,
) -> None:
    session = make_session(vu_index)
    try:
        while time.time() < deadline:
            try:
                await iteration_fn(session)
            except Exception as exc:  # a bug in journey code itself, not an HTTP failure
                errors.append(f"vu[{vu_index}] iteration raised: {type(exc).__name__}: {exc}")
            if time.time() >= deadline:
                break
    finally:
        await session.aclose()


async def run_for_duration(
    num_vus: int,
    duration_seconds: float,
    make_session: Callable[[int], Session],
    iteration_fn: IterationFn,
) -> tuple[float, list]:
    """Runs num_vus concurrent virtual users, each looping iteration_fn
    until the wall-clock duration elapses. Returns (actual_elapsed_seconds,
    harness_errors) -- harness_errors are bugs in the harness's own journey
    code (unexpected exceptions), never HTTP-level failures, which are
    already captured as unexpected_failure records in the collector."""
    start = time.time()
    deadline = start + duration_seconds
    errors: list = []
    tasks = [
        asyncio.create_task(_vu_duration_loop(i, make_session, iteration_fn, deadline, errors))
        for i in range(num_vus)
    ]
    await asyncio.gather(*tasks)
    return time.time() - start, errors


async def _vu_attempt_worker(
    vu_index: int,
    make_session: Callable[[int], Session],
    attempt_fn: IterationFn,
    attempt_count: int,
    errors: list,
) -> None:
    session = make_session(vu_index)
    try:
        for _ in range(attempt_count):
            try:
                await attempt_fn(session)
            except Exception as exc:
                errors.append(f"vu[{vu_index}] attempt raised: {type(exc).__name__}: {exc}")
    finally:
        await session.aclose()


async def run_fixed_attempts(
    total_attempts: int,
    num_vus: int,
    make_session: Callable[[int], Session],
    attempt_fn: IterationFn,
) -> tuple[float, list]:
    """Runs exactly total_attempts calls to attempt_fn spread as evenly as
    possible across num_vus concurrent workers (Profile B: "N transaction-
    create attempts at M concurrency"). Returns (elapsed_seconds, errors)."""
    base, remainder = divmod(total_attempts, num_vus)
    per_worker = [base + (1 if i < remainder else 0) for i in range(num_vus)]
    start = time.time()
    errors: list = []
    tasks = [
        asyncio.create_task(_vu_attempt_worker(i, make_session, attempt_fn, per_worker[i], errors))
        for i in range(num_vus)
        if per_worker[i] > 0
    ]
    await asyncio.gather(*tasks)
    return time.time() - start, errors
