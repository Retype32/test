"""Bulk-seeds a large volume of transactions directly through the app's own
ORM models, bypassing HTTP entirely (plan section 11: "seeding 200,000 rows
through the wizard's 14-request flow would itself take longer than the
test"). Used by Profile F's large-batch scenarios.

Runs as a standalone subprocess (`python -m tools.loadtest.bulk_seed ...`)
rather than being imported into the harness's long-lived process, so it
gets its own fresh `backend.core.config.Settings()` singleton bound to the
target DB directory via env vars -- the same reason tests/conftest.py sets
DATABASE_URL_* before importing anything backend-related.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import uuid
from datetime import datetime

_DUMMY_SEED_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--catalog", required=True, choices=["vms", "dayshift", "complete", "esnf"])
    parser.add_argument("--business-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--customer-id", default="C001")
    parser.add_argument("--location-id", default="L001")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args()


def _set_env(db_dir: str) -> None:
    os.environ.setdefault("DATABASE_URL_CORE", f"sqlite+aiosqlite:///{db_dir}/core.db")
    for name in ("vms", "dayshift", "complete", "esnf"):
        os.environ.setdefault(f"DATABASE_URL_{name.upper()}", f"sqlite+aiosqlite:///{db_dir}/catalog_{name}.db")
    os.environ.setdefault("SECRET_KEY", "loadtest-bulk-seed-not-for-production")


async def _seed(args) -> None:
    from backend.core.catalogs import CatalogCode
    from backend.core.database import get_catalog_sessionmaker, init_databases
    from backend.models.transaction import BalanceStatus, Denomination, Transaction

    await init_databases()
    code = CatalogCode(args.catalog)
    sessionmaker = get_catalog_sessionmaker(code)
    business_date = datetime.strptime(args.business_date, "%Y-%m-%d").date()
    rng = random.Random(12345)

    denom_choices = [("€50", 50), ("€20", 20), ("€10", 10), ("€100", 100)]

    inserted = 0
    async with sessionmaker() as session:
        for i in range(args.count):
            bag_number = "310" + f"{rng.randint(0, 10 ** 10 - 1):010d}"
            label, unit_value = rng.choice(denom_choices)
            count = rng.randint(1, 8)
            value = unit_value * count
            txn = Transaction(
                user_id=_DUMMY_SEED_USER_ID,
                username="loadtest_bulk_seed",
                customer_id=args.customer_id,
                location_id=args.location_id,
                bag_number=bag_number,
                wallet_id=f"WALLET-{uuid.uuid4().hex[:8]}",
                total_value=value,
                balance_status=BalanceStatus.balanced,
                expected_total=value,
                business_date=business_date,
            )
            txn.denominations.append(Denomination(denomination=label, count=count, value=value))
            session.add(txn)
            inserted += 1
            if inserted % args.batch_size == 0:
                await session.commit()
                print(f"  seeded {inserted}/{args.count}", flush=True)
        await session.commit()
    print(f"DONE seeded={inserted}", flush=True)


def main() -> None:
    args = _parse_args()
    _set_env(args.db_dir)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, project_root)
    asyncio.run(_seed(args))


if __name__ == "__main__":
    main()
