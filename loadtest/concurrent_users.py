"""Concurrent-users crash test.

Simulates increasing numbers of simultaneous logged-in users, each running a
realistic cashier loop (log in once, then repeatedly create a transaction and
read it back), and reports where throughput/latency/error-rate stop holding
up. Answers: "how many users at once can the program hold?"

Usage:
    python -m loadtest.concurrent_users --base-url http://127.0.0.1:8000 \\
        --levels 10,25,50,100,200,400 --actions-per-user 5

Run against a disposable database -- see loadtest/README.md.
"""
import argparse
import asyncio
import sys
import time

import httpx

from .common import (
    DEFAULT_CATALOG, SEEDED_USERS, ResultSet, login, random_transaction_payload, timed_request,
)

ERROR_RATE_LIMIT = 0.02       # stop ramping once >2% of requests fail
P95_LATENCY_LIMIT_S = 3.0     # or once p95 latency exceeds this


async def virtual_user(client: httpx.AsyncClient, base_url: str, username: str, password: str,
                        actions: int, catalog: str, results: ResultSet):
    from .common import RequestResult
    try:
        token = await login(client, base_url, username, password)
    except Exception as e:
        results.add(RequestResult(False, None, 0.0, error=f"login failed: {e}"))
        return
    headers = {"Authorization": f"Bearer {token}", "X-Catalog": catalog}

    for _ in range(actions):
        payload = random_transaction_payload(tag="CU")
        start = time.perf_counter()
        try:
            resp = await client.post(f"{base_url}/api/v1/transactions/", json=payload, headers=headers)
            latency = time.perf_counter() - start
            if resp.status_code < 400:
                results.add(RequestResult(True, resp.status_code, latency))
                txn_id = resp.json()["transaction_id"]
                r2 = await timed_request(
                    client.get(f"{base_url}/api/v1/transactions/{txn_id}", headers=headers)
                )
                results.add(r2)
            else:
                results.add(RequestResult(False, resp.status_code, latency, error=f"HTTP {resp.status_code}: {resp.text[:120]}"))
        except Exception as e:
            latency = time.perf_counter() - start
            results.add(RequestResult(False, None, latency, error=f"{type(e).__name__}: {e}"))


async def run_level(base_url: str, concurrency: int, actions_per_user: int, catalog: str) -> ResultSet:
    results = ResultSet(label=f"{concurrency} concurrent users")
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency + 10)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        start = time.perf_counter()
        tasks = []
        for i in range(concurrency):
            username, password = SEEDED_USERS[i % len(SEEDED_USERS)]
            tasks.append(virtual_user(client, base_url, username, password, actions_per_user, catalog, results))
        await asyncio.gather(*tasks)
        results.wall_time = time.perf_counter() - start
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--levels", default="10,25,50,100,200,400",
                         help="comma-separated concurrent-user counts to ramp through")
    parser.add_argument("--actions-per-user", type=int, default=5)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    args = parser.parse_args()

    levels = [int(x) for x in args.levels.split(",")]

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            health = await client.get(f"{args.base_url}/health")
            health.raise_for_status()
        except Exception as e:
            print(f"Server not reachable at {args.base_url}: {e}", file=sys.stderr)
            sys.exit(1)

    print(f"Target: {args.base_url}  |  levels: {levels}  |  actions/user: {args.actions_per_user}")

    breaking_point = None
    for level in levels:
        result = await run_level(args.base_url, level, args.actions_per_user, args.catalog)
        result.print_summary()
        p95 = result.latency_percentiles()["p95"]
        if result.error_rate > ERROR_RATE_LIMIT or (p95 is not None and p95 > P95_LATENCY_LIMIT_S):
            breaking_point = level
            print(f"\n>>> Threshold breached at {level} concurrent users "
                  f"(error rate {result.error_rate:.1%}, p95 {p95*1000 if p95 else 'n/a'} ms) -- stopping ramp.")
            break

    print("\n" + "=" * 60)
    if breaking_point:
        print(f"RESULT: comfortably held up to below {breaking_point} concurrent users "
              f"on this run; degraded at {breaking_point}.")
    else:
        print(f"RESULT: held up cleanly through the highest tested level ({levels[-1]}) -- "
              f"re-run with higher --levels to find the actual ceiling.")


if __name__ == "__main__":
    asyncio.run(main())
