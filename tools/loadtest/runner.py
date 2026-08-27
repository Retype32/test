"""Shared plumbing every profile (A-F) uses: wrap one drive_coro (a
VU-concurrency call from vu.py) with resource sampling, disk snapshots,
post-run DB integrity checks, and section-9 JSON+Markdown report writing.
Keeping this in one place means every profile's run report is structurally
identical, which is the whole point of the schema (plan section 9:
"so results are comparable across profiles and over time")."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from . import config
from .integrity import catalog_db_path, core_db_path, run_integrity_checks
from .metrics import MetricsCollector
from .report import build_run_summary, write_run_report
from .resources import ResourceSampler, db_file_sizes_mb, disk_free_mb

DriveFn = Callable[[MetricsCollector], Awaitable[tuple[float, list]]]


async def execute_run(
    *,
    run_id: str,
    workload_profile: str,
    profile_variant: str,
    target_vus: int,
    catalogs_used: list[str],
    drive: DriveFn,
    db_dir: Optional[str] = None,
    server_pid: Optional[int] = None,
    resource_interval: float = 2.0,
    integrity_catalog: Optional[str] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
    db_backend: str = "sqlite",
    notes_prefix: str = "",
) -> dict:
    collector = MetricsCollector()
    sampler = ResourceSampler(server_pid, interval_seconds=resource_interval)

    db_files_start = db_file_sizes_mb(db_dir)
    disk_free_start = disk_free_mb(db_dir)
    started_at = datetime.now(timezone.utc)

    await sampler.start()
    elapsed, harness_errors = await drive(collector)
    await sampler.stop()

    finished_at = datetime.now(timezone.utc)
    db_files_end = db_file_sizes_mb(db_dir)
    disk_free_end = disk_free_mb(db_dir)

    integrity = None
    if db_dir and integrity_catalog:
        integrity = await run_integrity_checks(
            catalog_db_path(db_dir, integrity_catalog),
            core_db_path(db_dir),
            collector.claimed_transaction_ids,
        )

    notes = notes_prefix
    if harness_errors:
        notes += (
            f" Harness-internal errors during this run ({len(harness_errors)} total, first 5 shown): "
            + "; ".join(harness_errors[:5])
        )
    if not db_dir:
        notes += " db_dir not provided -- disk metrics and data-integrity reconciliation skipped."
    if not server_pid:
        notes += " server_pid not provided -- CPU/memory resource sampling skipped."

    summary = build_run_summary(
        run_id=run_id,
        workload_profile=workload_profile,
        profile_variant=profile_variant,
        started_at=started_at,
        finished_at=finished_at,
        collector=collector,
        target_vus=target_vus,
        catalogs_used=catalogs_used,
        resource_summary=sampler.summary(db_dir),
        db_files_mb_start=db_files_start,
        db_files_mb_end=db_files_end,
        disk_free_mb_start=disk_free_start,
        disk_free_mb_end=disk_free_end,
        integrity=integrity,
        hardware_note=hardware_note,
        db_backend=db_backend,
        notes=notes.strip(),
    )
    json_path, md_path = write_run_report(summary, out_dir)
    return {"summary": summary, "json_path": json_path, "md_path": md_path, "collector": collector}


def make_run_id(profile_variant: str) -> str:
    return f"{profile_variant}-{int(time.time())}"
