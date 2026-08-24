"""Batch transaction-volume crash test.

Fires a large total number of transaction-create requests at a fixed
concurrency against a running server, to answer: "can the program hold up
across a batch of 2-3k transactions?" Reports throughput, error rate, and
specifically flags SQLite "database is locked" failures, which are the
expected failure mode for a single SQLite file under concurrent writers.

Usage:
    python -m loadtest.batch_transactions --base-url http://127.0.0.1:8000 \\
        --total 3000 --concurrency 50
"""
import argparse
import asyncio
import sys
import time

import httpx

from .common import DEFAULT_CATALOG, SEEDED_USERS, RequestResult, ResultSet, login, random_transaction_payload


async def worker(client: httpx.AsyncClient, base_url: str, headers_pool: list[dict],
                  sem: asyncio.Semaphore, index: int, results: ResultSet):
    headers = headers_pool[index % len(headers_pool)]
    async with sem:
        payload = random_transaction_payload(tag="BATCH")
        start = time.perf_counter()
        try:
            resp = await client.post(f"{base_url}/api/v1/transactions/", json=payload, headers=headers)
            latency = time.perf_counter() - start
            if resp.status_code < 400:
                results.add(RequestResult(True, resp.status_code, latency))
            else:
                detail = ""
                try:
                    detail = str(resp.json().get("detail", ""))[:150]
                except Exception:
                    detail = resp.text[:150]
                results.add(RequestResult(False, resp.status_code, latency, error=f"HTTP {resp.status_code}: {detail}"))
        except Exception as e:
            latency = time.perf_counter() - start
            results.add(RequestResult(False, None, latency, error=f"{type(e).__name__}: {e}"))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--total", type=int, default=3000, help="total transactions to submit")
    parser.add_argument("--concurrency", type=int, default=50, help="in-flight requests at once")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            health = await client.get(f"{args.base_url}/health")
            health.raise_for_status()
        except Exception as e:
            print(f"Server not reachable at {args.base_url}: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Logging in {len(SEEDED_USERS)} seeded users...")
        headers_pool = []
        for username, password in SEEDED_USERS:
            token = await login(client, args.base_url, username, password)
            headers_pool.append({"Authorization": f"Bearer {token}", "X-Catalog": args.catalog})

    print(f"Target: {args.base_url}  |  total: {args.total}  |  concurrency: {args.concurrency}")

    results = ResultSet(label=f"{args.total} transactions @ concurrency {args.concurrency}")
    limits = httpx.Limits(max_connections=args.concurrency + 10, max_keepalive_connections=args.concurrency + 10)
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        start = time.perf_counter()
        tasks = [worker(client, args.base_url, headers_pool, sem, i, results) for i in range(args.total)]

        # Progress reporting: report every ~10% completed.
        checkpoint = max(1, args.total // 10)
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % checkpoint == 0:
                elapsed = time.perf_counter() - start
                print(f"  {done}/{args.total} done  ({done/elapsed:.1f} req/s so far, "
                      f"{results.failures} failures)")

        results.wall_time = time.perf_counter() - start

    results.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
