"""Profile D -- spike test (plan section 5). Implemented as three
sequential phases (baseline stabilization -> spike -> cooldown), each its
own run report, so latency/error/pool-recovery behavior is comparable
before/during/after the spike without needing a single report to encode a
time-varying VU count. Callable per the task brief; intentionally the
simplest correct reading of the plan's three-phase description rather than
a single continuously-ramping driver."""
from __future__ import annotations

from typing import Optional

from .. import config, journeys, setup
from ..runner import execute_run, make_run_id
from ..vu import Session, run_for_duration


async def _run_phase(
    base_url, vus, catalog, duration, accounts, think_min, think_max,
    db_dir, server_pid, out_dir, hardware_note, phase_name, notes,
):
    async def drive(collector):
        def make_session(i: int) -> Session:
            username, password = accounts[i % len(accounts)]
            return Session(
                base_url=base_url, collector=collector, vu_id=f"D-{phase_name}-{i}",
                username=username, password=password, catalog=catalog,
                think_min=think_min, think_max=think_max,
            )
        return await run_for_duration(vus, duration, make_session, journeys.journey_j1_wizard)

    return await execute_run(
        run_id=make_run_id(f"D-{phase_name}-{vus}vu"),
        workload_profile="D",
        profile_variant=f"D-{phase_name}-{vus}vu-{catalog}",
        target_vus=vus,
        catalogs_used=[catalog],
        drive=drive,
        db_dir=db_dir,
        server_pid=server_pid,
        integrity_catalog=catalog,
        out_dir=out_dir,
        hardware_note=hardware_note,
        notes_prefix=notes,
    )


async def run_profile_d(
    base_url: str,
    baseline_vus: int = 25,
    spike_multiplier: float = 2.0,
    baseline_duration: float = 300.0,
    spike_duration: float = 300.0,
    cooldown_duration: float = 300.0,
    catalog: str = "vms",
    think_min: float = config.DEFAULT_THINK_TIME_MIN,
    think_max: float = config.DEFAULT_THINK_TIME_MAX,
    db_dir: Optional[str] = None,
    server_pid: Optional[int] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
) -> list[dict]:
    spike_vus = max(baseline_vus + 1, round(baseline_vus * spike_multiplier))
    accounts = await setup.ensure_cashier_accounts(base_url, spike_vus)

    results = []
    results.append(await _run_phase(
        base_url, baseline_vus, catalog, baseline_duration, accounts, think_min, think_max,
        db_dir, server_pid, out_dir, hardware_note, "baseline",
        f"Baseline stabilization phase before the spike ({baseline_vus} VUs, plan section 5).",
    ))
    results.append(await _run_phase(
        base_url, spike_vus, catalog, spike_duration, accounts, think_min, think_max,
        db_dir, server_pid, out_dir, hardware_note, "spike",
        f"Spike phase: {spike_vus} VUs (>= 2x the {baseline_vus}-VU baseline, plan section 5). "
        "Watch for pool-exhaustion/timeout root causes in errors_by_endpoint_and_cause.",
    ))
    results.append(await _run_phase(
        base_url, baseline_vus, catalog, cooldown_duration, accounts, think_min, think_max,
        db_dir, server_pid, out_dir, hardware_note, "cooldown",
        "Cooldown phase: back to baseline VUs. Compare this phase's latency/resource numbers "
        "against the baseline phase above -- a sustained gap indicates a resource "
        "(connection, memory) that did not release cleanly after the spike.",
    ))
    return results
