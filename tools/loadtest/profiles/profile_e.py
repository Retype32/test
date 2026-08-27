"""Profile E -- soak/endurance test (plan section 5). Reuses Profile C's
weighted mix at a concurrency comfortably below peak, for a long duration,
sampling resources throughout (the sampler already runs at a fixed
interval for the whole window via runner.execute_run/ResourceSampler).

Callable per the task brief. The plan's 2-4h initial target (let alone the
optional 8-12h overnight stretch goal) is far beyond what this sandbox
session can execute end-to-end -- default duration here is intentionally
short (10 minutes) so the function is genuinely runnable in a pinch; pass
--duration explicitly for a real soak run in Phase 5."""
from __future__ import annotations

from typing import Optional

from .. import config
from .profile_c import run_profile_c


async def run_profile_e(
    base_url: str,
    total_vus: int = 12,
    catalog: str = "vms",
    duration_seconds: float = 600.0,
    resource_interval: float = 60.0,
    think_min: float = config.DEFAULT_THINK_TIME_MIN,
    think_max: float = config.DEFAULT_THINK_TIME_MAX,
    db_dir: Optional[str] = None,
    server_pid: Optional[int] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
) -> dict:
    result = await run_profile_c(
        base_url=base_url, total_vus=total_vus, catalog=catalog, duration_seconds=duration_seconds,
        think_min=think_min, think_max=think_max, db_dir=db_dir, server_pid=server_pid,
        out_dir=out_dir, hardware_note=hardware_note,
        workload_profile="E", variant_prefix="E-soak", resource_interval=resource_interval,
    )
    return result
