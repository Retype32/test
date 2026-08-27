"""Profile A -- baseline load (plan section 5).

Concurrency tiers 1/5/10/25/50 VUs running J1 (cashier wizard), plus a
dedicated login-only sub-run per tier, each tier repeated `reps` times
(first rep discarded as warm-up), reported as one JSON+Markdown run report
per rep plus one small aggregate (median/IQR) per tier."""
from __future__ import annotations

import json
import os
from typing import Optional

from .. import config, journeys, setup
from ..metrics import median_iqr
from ..runner import execute_run, make_run_id
from ..vu import Session, run_for_duration


def _make_session_factory(base_url, catalogs, accounts, think_min, think_max, tag):
    def factory(collector):
        def make_session(i: int) -> Session:
            username, password = accounts[i % len(accounts)]
            catalog = catalogs[i % len(catalogs)]
            return Session(
                base_url=base_url, collector=collector, vu_id=f"{tag}-{i}",
                username=username, password=password, catalog=catalog,
                think_min=think_min, think_max=think_max,
            )
        return make_session
    return factory


async def run_profile_a(
    base_url: str,
    tiers: list[int] = (1, 5, 10, 25, 50),
    catalog: str = "vms",
    reps: int = 5,
    duration_seconds: float = 180.0,
    warmup_discard: bool = True,
    run_login_only: bool = True,
    login_only_reps: int = 1,
    login_only_duration: Optional[float] = None,
    think_min: float = config.DEFAULT_THINK_TIME_MIN,
    think_max: float = config.DEFAULT_THINK_TIME_MAX,
    db_dir: Optional[str] = None,
    server_pid: Optional[int] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
    multi_catalog_at_max: bool = False,
) -> list[dict]:
    tiers = list(tiers)
    max_vus = max(tiers)
    login_only_duration = login_only_duration if login_only_duration is not None else min(duration_seconds, 60.0)

    accounts = await setup.ensure_cashier_accounts(base_url, max_vus)

    all_results: list[dict] = []

    for tier in tiers:
        rep_results = []
        for rep in range(1, reps + 1):
            factory = _make_session_factory(base_url, [catalog], accounts, think_min, think_max, f"A{tier}r{rep}")

            async def drive(collector, factory=factory, tier=tier):
                make_session = factory(collector)
                return await run_for_duration(tier, duration_seconds, make_session, journeys.journey_j1_wizard)

            is_warmup = warmup_discard and rep == 1
            result = await execute_run(
                run_id=make_run_id(f"A-{tier}vu-{catalog}-rep{rep}"),
                workload_profile="A",
                profile_variant=f"A-{tier}vu-{catalog}-rep{rep}",
                target_vus=tier,
                catalogs_used=[catalog],
                drive=drive,
                db_dir=db_dir,
                server_pid=server_pid,
                integrity_catalog=catalog,
                out_dir=out_dir,
                hardware_note=hardware_note,
                notes_prefix=("WARM-UP REP (plan section 5) -- excluded from the tier's "
                              "median/IQR aggregate below." if is_warmup else ""),
            )
            result["is_warmup"] = is_warmup
            rep_results.append(result)

        all_results.extend(rep_results)
        kept = [r for r in rep_results if not r["is_warmup"]] or rep_results
        _write_tier_aggregate(kept, tier, catalog, out_dir, "A")

        if run_login_only:
            for rep in range(1, login_only_reps + 1):
                factory = _make_session_factory(
                    base_url, [catalog], accounts, think_min, think_max, f"Alogin{tier}r{rep}"
                )

                async def drive_login(collector, factory=factory, tier=tier):
                    make_session = factory(collector)
                    return await run_for_duration(
                        tier, login_only_duration, make_session, journeys.journey_j1_login_only
                    )

                result = await execute_run(
                    run_id=make_run_id(f"A-{tier}vu-login-only-rep{rep}"),
                    workload_profile="A",
                    profile_variant=f"A-{tier}vu-login-only-rep{rep}",
                    target_vus=tier,
                    catalogs_used=[catalog],
                    drive=drive_login,
                    db_dir=db_dir,
                    server_pid=server_pid,
                    integrity_catalog=None,  # login-only never writes transactions
                    out_dir=out_dir,
                    hardware_note=hardware_note,
                    notes_prefix="Login-only sub-case: isolates the synchronous bcrypt "
                                 "cost (plan section 2) from the rest of J1.",
                )
                all_results.append(result)

    if multi_catalog_at_max:
        factory = _make_session_factory(
            base_url, list(config.CATALOGS), accounts, think_min, think_max, f"Amulti{max_vus}"
        )

        async def drive_multi(collector, factory=factory):
            make_session = factory(collector)
            return await run_for_duration(max_vus, duration_seconds, make_session, journeys.journey_j1_wizard)

        result = await execute_run(
            run_id=make_run_id(f"A-{max_vus}vu-multi-catalog"),
            workload_profile="A",
            profile_variant=f"A-{max_vus}vu-multi-catalog-rep1",
            target_vus=max_vus,
            catalogs_used=list(config.CATALOGS),
            drive=drive_multi,
            db_dir=db_dir,
            server_pid=server_pid,
            integrity_catalog=None,  # spread across 4 catalogs -- no single integrity target
            out_dir=out_dir,
            hardware_note=hardware_note,
            notes_prefix="Multi-catalog spread at the top tier (plan section 5): VUs distributed "
                         "evenly across all 4 catalogs to see whether the 4-engine split parallelizes.",
        )
        all_results.append(result)

    return all_results


def _write_tier_aggregate(kept: list[dict], tier: int, catalog: str, out_dir: str, profile: str) -> None:
    rps_vals = [r["summary"]["throughput"]["rps_mean"] for r in kept]
    tps_vals = [r["summary"]["throughput"]["tps_mean"] for r in kept]
    p95_vals = [r["summary"]["latency_ms"]["overall"]["p95"] for r in kept]
    p99_vals = [r["summary"]["latency_ms"]["overall"]["p99"] for r in kept]
    agg = {
        "profile": profile,
        "tier_vus": tier,
        "catalog": catalog,
        "reps_kept": len(kept),
        "rps_mean": median_iqr(rps_vals),
        "tps_mean": median_iqr(tps_vals),
        "p95_overall_ms": median_iqr(p95_vals),
        "p99_overall_ms": median_iqr(p99_vals),
        "rep_json_files": [r["json_path"] for r in kept],
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{profile}-{tier}vu-{catalog}-tier-summary.json")
    with open(path, "w") as f:
        json.dump(agg, f, indent=2)
