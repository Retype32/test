"""CLI entrypoint for the load-test harness.

Examples:
    python -m tools.loadtest --profile A --tiers 1,5,10 --catalog vms \\
        --base-url http://127.0.0.1:8000 --db-dir /tmp/nexus_loadtest_db \\
        --server-pid 12345

    python -m tools.loadtest --profile B --cases 500:5,1000:10,3000:25,5000:50 \\
        --base-url http://127.0.0.1:8000 --db-dir /tmp/nexus_loadtest_db

    python -m tools.loadtest --profile F1 --base-url http://127.0.0.1:8000 \\
        --db-dir /tmp/nexus_loadtest_db --row-count 5000

The server is assumed to already be running (a real `uvicorn
backend.main:app --reload=False`, per the plan) -- this harness never
spawns it itself; point --base-url at wherever you started it, and
--db-dir at the directory it's writing its SQLite files into (needed for
disk metrics and post-run data-integrity reconciliation), and --server-pid
at its process ID (needed for CPU/memory resource sampling). Both are
optional -- omit either and that section of the report says
"not_measured" rather than failing.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from . import config
from .setup import wait_for_server


def _parse_tiers(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_cases(raw: str) -> list[tuple[int, int]]:
    cases = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        attempts, vus = part.split(":")
        cases.append((int(attempts), int(vus)))
    return cases


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.loadtest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", required=True,
                   choices=["A", "B", "C", "D", "E", "F1", "F2", "F3", "F4"],
                   help="Workload profile to run (plan section 5). F is split into F1-F4 "
                        "sub-scenarios -- see profiles/profile_f.py.")
    p.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8000")
    p.add_argument("--catalog", default="vms", choices=list(config.CATALOGS))
    p.add_argument("--db-dir", default=None,
                   help="Directory the server's SQLite .db files live in (disk metrics + "
                        "post-run integrity reconciliation; omit to skip both).")
    p.add_argument("--server-pid", type=int, default=None,
                   help="PID of the uvicorn process (CPU/memory sampling via psutil; "
                        "omit to skip resource sampling).")
    p.add_argument("--out-dir", default="tools/loadtest/results")
    p.add_argument("--hardware-note", default=config.DEFAULT_HARDWARE_NOTE)
    p.add_argument("--no-wait-for-server", action="store_true",
                   help="Skip polling the server for readiness before starting.")

    # Profile A
    p.add_argument("--tiers", default="1,5,10,25,50", help="A: comma-separated VU counts")
    p.add_argument("--reps", type=int, default=5, help="A: repetitions per tier")
    p.add_argument("--duration", type=float, default=180.0, help="A/C/D/F2/F3: seconds per run/phase")
    p.add_argument("--no-warmup-discard", action="store_true", help="A: keep rep 1 in the aggregate")
    p.add_argument("--no-login-only", action="store_true", help="A: skip the login-only sub-run")
    p.add_argument("--login-only-reps", type=int, default=1)
    p.add_argument("--multi-catalog", action="store_true",
                   help="A: also run one extra rep at the top tier spread across all 4 catalogs")

    # Profile B
    p.add_argument("--cases", default="500:5,1000:10,3000:25,5000:50",
                   help="B: comma-separated attempts:vus pairs, e.g. 500:5,1000:10")

    # Profile C / E
    p.add_argument("--total-vus", type=int, default=25, help="C/E: total VU pool size")

    # Profile D
    p.add_argument("--baseline-vus", type=int, default=25)
    p.add_argument("--spike-multiplier", type=float, default=2.0)
    p.add_argument("--baseline-duration", type=float, default=300.0)
    p.add_argument("--spike-duration", type=float, default=300.0)
    p.add_argument("--cooldown-duration", type=float, default=300.0)

    # Profile F1
    p.add_argument("--row-count", type=int, default=5000, help="F1: rows to bulk-seed")
    p.add_argument("--business-date", default=None, help="F1/F3: YYYY-MM-DD, default today")

    # Profile F2 / F3
    p.add_argument("--concurrent-downloads", type=int, default=5, help="F2")
    p.add_argument("--concurrency", type=int, default=5, help="F3")

    # Profile F4
    p.add_argument("--iterations", type=int, default=500, help="F4: parse_report calls")
    p.add_argument("--f4-concurrency", type=int, default=8, help="F4: asyncio.to_thread concurrency")

    # Shared timing knobs
    p.add_argument("--think-min", type=float, default=config.DEFAULT_THINK_TIME_MIN)
    p.add_argument("--think-max", type=float, default=config.DEFAULT_THINK_TIME_MAX)

    return p


def _business_date(raw: str | None):
    if not raw:
        return dt.date.today()
    return dt.date.fromisoformat(raw)


async def _run(args: argparse.Namespace) -> int:
    if not args.no_wait_for_server:
        ok = await wait_for_server(args.base_url)
        if not ok:
            print(f"ERROR: server at {args.base_url} did not respond within the readiness timeout.",
                  file=sys.stderr)
            return 2

    results: list = []

    if args.profile == "A":
        from .profiles.profile_a import run_profile_a
        results = await run_profile_a(
            base_url=args.base_url, tiers=_parse_tiers(args.tiers), catalog=args.catalog,
            reps=args.reps, duration_seconds=args.duration, warmup_discard=not args.no_warmup_discard,
            run_login_only=not args.no_login_only, login_only_reps=args.login_only_reps,
            think_min=args.think_min, think_max=args.think_max,
            db_dir=args.db_dir, server_pid=args.server_pid, out_dir=args.out_dir,
            hardware_note=args.hardware_note, multi_catalog_at_max=args.multi_catalog,
        )
    elif args.profile == "B":
        from .profiles.profile_b import run_profile_b
        results = await run_profile_b(
            base_url=args.base_url, cases=_parse_cases(args.cases), catalog=args.catalog,
            db_dir=args.db_dir, server_pid=args.server_pid, out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )
    elif args.profile == "C":
        from .profiles.profile_c import run_profile_c
        results = [await run_profile_c(
            base_url=args.base_url, total_vus=args.total_vus, catalog=args.catalog,
            duration_seconds=args.duration, think_min=args.think_min, think_max=args.think_max,
            db_dir=args.db_dir, server_pid=args.server_pid, out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )]
    elif args.profile == "D":
        from .profiles.profile_d import run_profile_d
        results = await run_profile_d(
            base_url=args.base_url, baseline_vus=args.baseline_vus, spike_multiplier=args.spike_multiplier,
            baseline_duration=args.baseline_duration, spike_duration=args.spike_duration,
            cooldown_duration=args.cooldown_duration, catalog=args.catalog,
            think_min=args.think_min, think_max=args.think_max,
            db_dir=args.db_dir, server_pid=args.server_pid, out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )
    elif args.profile == "E":
        from .profiles.profile_e import run_profile_e
        results = [await run_profile_e(
            base_url=args.base_url, total_vus=args.total_vus, catalog=args.catalog,
            duration_seconds=args.duration, think_min=args.think_min, think_max=args.think_max,
            db_dir=args.db_dir, server_pid=args.server_pid, out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )]
    elif args.profile == "F1":
        if not args.db_dir:
            print("ERROR: --db-dir is required for F1 (bulk seeding writes directly to the DB files).",
                  file=sys.stderr)
            return 2
        from .profiles.profile_f import run_profile_f1_eod_batch
        results = [await run_profile_f1_eod_batch(
            base_url=args.base_url, db_dir=args.db_dir, catalog=args.catalog, row_count=args.row_count,
            business_date=_business_date(args.business_date), out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )]
    elif args.profile == "F2":
        from .profiles.profile_f import run_profile_f2_report_concurrency
        results = [await run_profile_f2_report_concurrency(
            base_url=args.base_url, catalog=args.catalog, concurrent_downloads=args.concurrent_downloads,
            duration_seconds=args.duration, db_dir=args.db_dir, out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )]
    elif args.profile == "F3":
        from .profiles.profile_f import run_profile_f3_history_search
        results = [await run_profile_f3_history_search(
            base_url=args.base_url, catalog=args.catalog, concurrency=args.concurrency,
            duration_seconds=args.duration, db_dir=args.db_dir, out_dir=args.out_dir,
            hardware_note=args.hardware_note,
        )]
    elif args.profile == "F4":
        from .profiles.profile_f import run_profile_f4_hardware_parser_microbench
        results = [await run_profile_f4_hardware_parser_microbench(
            iterations=args.iterations, concurrency=args.f4_concurrency, out_dir=args.out_dir,
        )]

    print(f"\n{len(results)} run report(s) written to {args.out_dir}:")
    for r in results:
        json_path = r.get("json_path")
        if json_path:
            print(f"  {json_path}")
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
