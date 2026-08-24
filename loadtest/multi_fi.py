"""Multi-FI concurrent crash test.

Runs the same concurrent-user workload as concurrent_users.py against every
processing catalog ("FI") at once, to confirm that N concurrent users in one
catalog don't degrade another catalog carrying the same load at the same
time. Each catalog is backed by its own separate SQLite database file, so
(with WAL mode enabled -- see backend/core/database.py) they should scale
independently: N users/FI x 4 FIs is not the same contention as 4N users on
one database.

Usage:
    python -m loadtest.multi_fi --base-url http://127.0.0.1:8000 \\
        --users-per-fi 20 --actions-per-user 5
"""
import argparse
import asyncio
import sys

import httpx

from .common import ALL_CATALOGS, ResultSet
from .concurrent_users import run_level


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users-per-fi", type=int, default=20)
    parser.add_argument("--actions-per-user", type=int, default=5)
    parser.add_argument("--catalogs", default=",".join(ALL_CATALOGS),
                         help="comma-separated catalog/FI codes to load simultaneously")
    args = parser.parse_args()

    catalogs = args.catalogs.split(",")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            health = await client.get(f"{args.base_url}/health")
            health.raise_for_status()
        except Exception as e:
            print(f"Server not reachable at {args.base_url}: {e}", file=sys.stderr)
            sys.exit(1)

    total = args.users_per_fi * len(catalogs)
    print(f"Target: {args.base_url}  |  FIs: {catalogs}  |  users/FI: {args.users_per_fi}  "
          f"|  total concurrent users across all FIs: {total}")

    # All FIs are hit at once, from the same asyncio.gather -- this is the
    # "20 people in each FI at the same time" scenario, not 4 back-to-back
    # single-FI runs.
    results_per_catalog = await asyncio.gather(*[
        run_level(args.base_url, args.users_per_fi, args.actions_per_user, catalog)
        for catalog in catalogs
    ])

    combined = ResultSet(label=f"ALL {len(catalogs)} FIs combined ({total} concurrent users)")
    max_wall = 0.0
    for catalog, result in zip(catalogs, results_per_catalog):
        result.label = f"FI '{catalog}' ({args.users_per_fi} concurrent users)"
        result.print_summary()
        combined.results.extend(result.results)
        max_wall = max(max_wall, result.wall_time)
    combined.wall_time = max_wall
    combined.print_summary()

    if combined.error_rate > 0.02:
        print(f"\n>>> Combined error rate {combined.error_rate:.1%} exceeds 2% -- "
              f"the FIs are contending with each other or with a shared resource (e.g. core.db logins).")
    else:
        print(f"\nRESULT: held up cleanly with {args.users_per_fi} concurrent users in "
              f"each of {len(catalogs)} FIs at once ({total} total).")


if __name__ == "__main__":
    asyncio.run(main())
