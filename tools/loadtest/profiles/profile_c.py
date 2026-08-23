"""Profile C -- realistic workflow chain (plan section 5): the weighted J1/
J2/J3/J4/J5/J6 mix (65/5/15/8/4/3, plan's proposed starting weights),
sustained for a configurable window. Implemented as callable per the task
brief but intentionally not gold-plated: VUs are split into a cashier pool
(runs J1 every iteration) and a supervisor/admin pool (each iteration
picks one of J2/J3/J4/J5/J6 by the plan's relative weights) rather than
literally re-deriving a role per iteration -- a pragmatic reading of
"weighted mix per VU-minute" given this app's role-gated routes."""
from __future__ import annotations

import random
from datetime import date
from typing import Optional

from .. import config, journeys, setup
from ..runner import execute_run, make_run_id
from ..vu import Session, run_for_duration

# plan section 5, Profile C table (J1 excluded from this sub-weighting --
# it drives the cashier-pool sizing instead, see run_profile_c below).
SUPERVISOR_JOURNEY_WEIGHTS = {
    "J3": 0.15,
    "J2": 0.05,
    "J4": 0.08,
    "J5": 0.04,
    "J6": 0.03,
}
J1_WEIGHT = 0.65


async def _supervisor_iteration(session: Session, rng: random.Random, business_date: date) -> None:
    names = list(SUPERVISOR_JOURNEY_WEIGHTS.keys())
    weights = list(SUPERVISOR_JOURNEY_WEIGHTS.values())
    choice = rng.choices(names, weights=weights, k=1)[0]
    if choice == "J2":
        await journeys.journey_j2_eod(session, business_date)
    elif choice == "J3":
        await journeys.journey_j3_search_review_correct(session)
    elif choice == "J4":
        await journeys.journey_j4_report_export(session, "xlsx" if rng.random() < 0.5 else "csv")
    elif choice == "J5":
        await journeys.journey_j5_duplicate_review(session)
    elif choice == "J6":
        await journeys.journey_j6_notifications(session)
    await session.think()


async def run_profile_c(
    base_url: str,
    total_vus: int = 25,
    catalog: str = "vms",
    duration_seconds: float = 3600.0,
    think_min: float = config.DEFAULT_THINK_TIME_MIN,
    think_max: float = config.DEFAULT_THINK_TIME_MAX,
    db_dir: Optional[str] = None,
    server_pid: Optional[int] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
    workload_profile: str = "C",
    variant_prefix: str = "C",
    resource_interval: float = 2.0,
) -> dict:
    n_cashier = max(1, round(total_vus * J1_WEIGHT))
    n_supervisor = max(1, total_vus - n_cashier) if total_vus > 1 else 0

    cashier_accounts = await setup.ensure_cashier_accounts(base_url, n_cashier)
    supervisor_accounts = await setup.ensure_supervisor_accounts(base_url, max(n_supervisor, 1))

    business_date = date.today()

    async def drive(collector):
        def make_session(i: int) -> Session:
            if i < n_cashier:
                username, password = cashier_accounts[i % len(cashier_accounts)]
            else:
                idx = i - n_cashier
                username, password = supervisor_accounts[idx % len(supervisor_accounts)]
            return Session(
                base_url=base_url, collector=collector, vu_id=f"C-{i}",
                username=username, password=password, catalog=catalog,
                think_min=think_min, think_max=think_max,
            )

        async def iteration(session: Session) -> None:
            # crude i<n_cashier re-derivation via username membership, since
            # Session doesn't carry a role flag -- cheap and good enough here.
            is_cashier = any(session.username == u for u, _ in cashier_accounts)
            if is_cashier:
                await journeys.journey_j1_wizard(session)
            else:
                await _supervisor_iteration(session, session.rng, business_date)

        return await run_for_duration(total_vus, duration_seconds, make_session, iteration)

    result = await execute_run(
        run_id=make_run_id(f"{variant_prefix}-{total_vus}vu-{catalog}"),
        workload_profile=workload_profile,
        profile_variant=f"{variant_prefix}-{total_vus}vu-{catalog}-mix",
        target_vus=total_vus,
        catalogs_used=[catalog],
        drive=drive,
        db_dir=db_dir,
        server_pid=server_pid,
        integrity_catalog=catalog,
        out_dir=out_dir,
        hardware_note=hardware_note,
        resource_interval=resource_interval,
        notes_prefix=(
            f"Weighted mix: {n_cashier} cashier VUs (J1), {n_supervisor} supervisor/admin VUs "
            f"(J2/J3/J4/J5/J6 at plan section 5's 5/15/8/4/3 relative weights). Starting-guess "
            "weights per plan section 12 open question 4 -- not measured real usage."
        ),
    )
    return result
