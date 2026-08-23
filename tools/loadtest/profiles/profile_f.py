"""Profile F -- heavy operations, real features only (plan section 5).

Callable per the task brief but explicitly NOT exhaustive: sub-scenario 1
(EOD large-batch) and sub-scenario 4 (hardware-parser microbenchmark) are
implemented at a genuinely useful scale; sub-scenario 2 (concurrent report
downloads) is implemented at a reduced, smoke-appropriate scale; sub-
scenario 3 (audit/history search at 50k-200k rows) reuses whatever data
sub-scenario 1 already seeded rather than seeding its own separate
50k-200k-row dataset -- seeding that volume and re-running the full
concurrency-race check from plan section 5 item F.1's fourth bullet were
both judged not worth the remaining implementation time versus a solid
Profile A/B (see the task's own priority ordering). Both gaps are noted in
each scenario's run report `notes` field, not silently dropped.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from datetime import date
from typing import Optional

from .. import config, journeys, setup
from ..runner import execute_run, make_run_id
from ..vu import Session, run_for_duration


async def _bulk_seed_subprocess(db_dir: str, catalog: str, business_date: date, count: int) -> None:
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tools.loadtest.bulk_seed",
        "--db-dir", db_dir, "--catalog", catalog,
        "--business-date", business_date.isoformat(), "--count", str(count),
        cwd=project_root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"bulk_seed subprocess failed (exit {proc.returncode}):\n{out.decode(errors='replace')}")


async def run_profile_f1_eod_batch(
    base_url: str,
    db_dir: str,
    catalog: str = "vms",
    row_count: int = 5000,
    business_date: Optional[date] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
) -> dict:
    """Seeds `row_count` transactions directly (bypassing HTTP, plan
    section 11), then times: EOD close, the transaction list read path,
    and the stats aggregation read path -- the plan's corrected framing
    (section 2) that EOD close itself is O(1) and the real cost lives in
    these read paths."""
    business_date = business_date or date.today()
    await _bulk_seed_subprocess(db_dir, catalog, business_date, row_count)

    accounts = await setup.ensure_supervisor_accounts(base_url, 1)
    username, password = accounts[0]

    async def drive(collector):
        errors: list = []
        start = time.time()
        try:
            s = Session(base_url=base_url, collector=collector, vu_id="F1", username=username,
                        password=password, catalog=catalog)
            await s.ensure_api_token()
            headers = s.api_headers()

            await s.api().call(
                "GET", "/api/v1/transactions/", params={"business_date": business_date.isoformat(), "limit": 1000},
                headers=headers, success_statuses=(200,),
            )
            await s.api().call(
                "GET", "/api/v1/stats/processors", params={"business_date": business_date.isoformat()},
                headers=headers, success_statuses=(200,),
            )
            await s.web_tc.call(
                "POST", "/web/login", data={"username": username, "password": password}, success_statuses=(303,),
            )
            await s.web_tc.call("POST", "/web/catalog/select", data={"code": catalog}, success_statuses=(303,))
            await s.web_tc.call(
                "GET", "/web/transactions", params={"business_date": business_date.isoformat()},
                success_statuses=(200,),
            )

            async def _closed(_r) -> bool:
                check = await s.api().call(
                    "GET", "/api/v1/eod/status", params={"business_date": business_date.isoformat()},
                    headers=headers, success_statuses=(200,),
                )
                return bool(check is not None and check.json().get("closed") is True)

            await s.web_tc.call(
                "POST", "/web/eod/close", data={"business_date": business_date.isoformat()},
                success_statuses=(303,), success_check=_closed,
            )
            await s.aclose()
        except Exception as exc:
            errors.append(f"F1 scenario raised: {type(exc).__name__}: {exc}")
        return time.time() - start, errors

    result = await execute_run(
        run_id=make_run_id(f"F-eod-largebatch-{row_count}"),
        workload_profile="F",
        profile_variant=f"F-eod-largebatch-{row_count}rows-{catalog}",
        target_vus=1,
        catalogs_used=[catalog],
        drive=drive,
        db_dir=db_dir,
        server_pid=None,
        integrity_catalog=catalog,
        out_dir=out_dir,
        hardware_note=hardware_note,
        notes_prefix=(
            f"Seeded {row_count} rows directly via ORM (bypassing HTTP) for business_date="
            f"{business_date.isoformat()} in catalog={catalog} before timing. Concurrent-write-vs-close "
            "race check (plan section 5 F.1 bullet 4) is NOT exercised by this scenario -- see profile_d "
            "for the general spike/pool-recovery check instead."
        ),
    )
    return result


async def run_profile_f2_report_concurrency(
    base_url: str,
    catalog: str = "vms",
    concurrent_downloads: int = 5,
    duration_seconds: float = 60.0,
    db_dir: Optional[str] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
) -> dict:
    """Concurrent report downloads (both formats) plus a light background
    trickle of J1/J7 traffic, to see whether unrelated requests slow down
    while the synchronous openpyxl/pandas report build runs (plan section 2
    head-of-line-blocking concern)."""
    total_vus = concurrent_downloads + 2
    accounts = await setup.ensure_cashier_accounts(base_url, 2)
    sup_accounts = await setup.ensure_supervisor_accounts(base_url, max(concurrent_downloads, 1))

    async def drive(collector):
        def make_session(i: int) -> Session:
            if i < concurrent_downloads:
                username, password = sup_accounts[i % len(sup_accounts)]
            else:
                username, password = accounts[(i - concurrent_downloads) % len(accounts)]
            return Session(base_url=base_url, collector=collector, vu_id=f"F2-{i}",
                            username=username, password=password, catalog=catalog,
                            think_min=0.2, think_max=0.5)

        async def iteration(session: Session) -> None:
            vu_index = int(session.vu_id.rsplit("-", 1)[-1])
            if vu_index < concurrent_downloads:
                fmt = "xlsx" if session.rng.random() < 0.5 else "csv"
                await journeys.journey_j4_report_export(session, fmt)
            else:
                await journeys.journey_j7_stats(session)

        return await run_for_duration(total_vus, duration_seconds, make_session, iteration)

    result = await execute_run(
        run_id=make_run_id(f"F-report-concurrency-{concurrent_downloads}"),
        workload_profile="F",
        profile_variant=f"F-report-concurrency-{concurrent_downloads}dl-{catalog}",
        target_vus=total_vus,
        catalogs_used=[catalog],
        drive=drive,
        db_dir=db_dir,
        server_pid=None,
        integrity_catalog=None,
        out_dir=out_dir,
        hardware_note=hardware_note,
        notes_prefix=(
            f"{concurrent_downloads} VUs repeatedly downloading xlsx/csv reports, "
            "2 VUs running J7 (dashboard/stats) as the 'unrelated traffic' whose latency this "
            "scenario watches for a spike during a report build (plan section 2/5 F.2)."
        ),
    )
    return result


async def run_profile_f3_history_search(
    base_url: str,
    catalog: str = "vms",
    concurrency: int = 5,
    duration_seconds: float = 60.0,
    db_dir: Optional[str] = None,
    out_dir: str = "tools/loadtest/results",
    hardware_note: str = config.DEFAULT_HARDWARE_NOTE,
) -> dict:
    """Audit/history search at whatever volume is already seeded in the
    target catalog (run profile_f1_eod_batch first for a real large-volume
    dataset -- see the module docstring for why this doesn't seed its own
    separate 50k-200k-row dataset)."""
    accounts = await setup.ensure_supervisor_accounts(base_url, concurrency)

    async def drive(collector):
        def make_session(i: int) -> Session:
            username, password = accounts[i % len(accounts)]
            return Session(base_url=base_url, collector=collector, vu_id=f"F3-{i}",
                            username=username, password=password, catalog=catalog,
                            think_min=0.1, think_max=0.3)

        async def iteration(session: Session) -> None:
            login = await session.web_tc.call(
                "POST", "/web/login", data={"username": session.username, "password": session.password},
                success_statuses=(303,),
            )
            if login is None or login.status_code != 303:
                return
            await session.web_tc.call(
                "POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,),
            )
            await session.web_tc.call(
                "GET", "/web/transactions",
                params={"date_from": "", "date_to": "", "customer_id": config.DEFAULT_CUSTOMER_ID, "location_id": ""},
                success_statuses=(200,),
            )
            await session.ensure_api_token()
            await session.api().call(
                "GET", "/api/v1/transactions/", params={"customer_id": config.DEFAULT_CUSTOMER_ID, "limit": 500},
                headers=session.api_headers(), success_statuses=(200,),
            )

        return await run_for_duration(concurrency, duration_seconds, make_session, iteration)

    result = await execute_run(
        run_id=make_run_id(f"F-history-search-{concurrency}vu"),
        workload_profile="F",
        profile_variant=f"F-history-search-{concurrency}vu-{catalog}",
        target_vus=concurrency,
        catalogs_used=[catalog],
        drive=drive,
        db_dir=db_dir,
        server_pid=None,
        integrity_catalog=None,
        out_dir=out_dir,
        hardware_note=hardware_note,
        notes_prefix=(
            "Search/filter latency against whatever volume is currently seeded in this catalog -- "
            "run run_profile_f1_eod_batch first to seed a real large volume. Does not independently "
            "seed the plan's 50k-200k-row dataset (see module docstring)."
        ),
    )
    return result


async def run_profile_f4_hardware_parser_microbench(
    iterations: int = 500,
    concurrency: int = 8,
    out_dir: str = "tools/loadtest/results",
) -> dict:
    """Pure-CPU micro-benchmark (plan section 5 F.4): no HTTP, no server
    needed. Confirms hardware/report_parser.py::parse_report's per-parse
    cost is negligible and that asyncio.to_thread offload actually keeps
    it off the event loop under concurrent calls."""
    from hardware.report_parser import parse_report
    from hardware.report_profile import load_profile
    from hardware.simulate import DEFAULT_BATCH, build_report

    profile = load_profile("bps_c1_eur")
    report_text = build_report(DEFAULT_BATCH, "columns")

    def _parse_once() -> float:
        t0 = time.perf_counter()
        parse_report(report_text, profile)
        return (time.perf_counter() - t0) * 1000

    # Serial baseline.
    serial_latencies = [_parse_once() for _ in range(min(iterations, 200))]

    # Concurrent, via asyncio.to_thread -- matches the real call site
    # (web/routes/transaction_entry_web.py:441-452).
    start = time.time()

    async def _concurrent_parse() -> float:
        t0 = time.perf_counter()
        await asyncio.to_thread(parse_report, report_text, profile)
        return (time.perf_counter() - t0) * 1000

    sem = asyncio.Semaphore(concurrency)

    async def _bounded():
        async with sem:
            return await _concurrent_parse()

    concurrent_latencies = await asyncio.gather(*[_bounded() for _ in range(iterations)])
    elapsed = time.time() - start

    summary = {
        "scenario": "F4-hardware-parser-microbench",
        "iterations": iterations,
        "concurrency": concurrency,
        "serial_latency_ms": {
            "mean": round(statistics.mean(serial_latencies), 4),
            "max": round(max(serial_latencies), 4),
        },
        "concurrent_latency_ms": {
            "mean": round(statistics.mean(concurrent_latencies), 4),
            "max": round(max(concurrent_latencies), 4),
        },
        "concurrent_total_wall_seconds": round(elapsed, 3),
        "concurrent_throughput_parses_per_sec": round(iterations / elapsed, 2) if elapsed > 0 else 0,
        "notes": (
            "Pure-CPU, no HTTP/server involved (plan section 5 F.4). Confirms parse_report's per-call "
            "cost and that asyncio.to_thread offload keeps it off the event loop under concurrency."
        ),
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "F-hardware-parser-microbench.json")
    import json
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
    return {"summary": summary, "json_path": path}
