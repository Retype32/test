"""Profile B -- transaction-creation ramp (plan section 5): total attempted
*volume* as the independent variable, not concurrency. Uses J1's API-only
equivalent (POST /api/v1/transactions/) to isolate business-logic/DB cost
from HTML rendering, per the plan.

Default case set is the "matched diagonal" the plan names as the pragmatic
minimum (500@5, 1000@10, 3000@25, 5000@50) -- the full 4x4 cross product is
supported by passing a longer `cases` list."""
from __future__ import annotations

from typing import Optional

from .. import config, journeys, setup
from ..runner import execute_run, make_run_id
from ..vu import Session, run_fixed_attempts

DEFAULT_DIAGONAL: list[tuple[int, int]] = [(500, 5), (1000, 10), (3000, 25), (5000, 50)]


async def run_profile_b(
    base_url: str,
    cases: list[tuple[int, int]] = tuple(DEFAULT_DIAGONAL),
    catalog: str = "vms",
    db_dir: Optional[str] = None,
    server_pid: Optional[int] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
) -> list[dict]:
    cases = list(cases)
    max_vus = max(vus for _, vus in cases)
    accounts = await setup.ensure_cashier_accounts(base_url, max_vus)

    results = []
    for attempts, vus in cases:
        def factory(collector, accounts=accounts, catalog=catalog, tag=f"B{attempts}x{vus}"):
            def make_session(i: int) -> Session:
                username, password = accounts[i % len(accounts)]
                return Session(
                    base_url=base_url, collector=collector, vu_id=f"{tag}-{i}",
                    username=username, password=password, catalog=catalog,
                    think_min=0.0, think_max=0.0,
                )
            return make_session

        async def drive(collector, factory=factory, attempts=attempts, vus=vus):
            make_session = factory(collector)

            async def attempt(session: Session) -> None:
                await journeys.journey_j1_api_create(session)

            return await run_fixed_attempts(attempts, vus, make_session, attempt)

        result = await execute_run(
            run_id=make_run_id(f"B-{attempts}attempts-{vus}vu"),
            workload_profile="B",
            profile_variant=f"B-{attempts}attempts-{vus}vu",
            target_vus=vus,
            catalogs_used=[catalog],
            drive=drive,
            db_dir=db_dir,
            server_pid=server_pid,
            integrity_catalog=catalog,
            out_dir=out_dir,
            hardware_note=hardware_note,
            notes_prefix=(
                f"{attempts} transaction-create attempts at {vus} VUs, API-only "
                "(bag/wallet IDs generated per tests/test_web_transaction_entry.py's own "
                "convention). A 'failed' attempt from a randomly-collided bag number "
                "is a valid_rejection, not a defect (plan section 5)."
            ),
        )
        results.append(result)
    return results
