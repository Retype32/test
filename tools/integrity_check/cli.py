"""CLI entry point -- argument parsing, orchestration across catalogs, and
the exit-code contract from
docs/production_readiness/04_postgresql_and_reconciliation.md §4.2:

    0 = all checks at/above --severity-threshold passed
    1 = at least one such check found a violation
    2 = tool/connection error (bad args, couldn't connect, couldn't query)
        -- never a stack trace, never conflated with "found a data problem"
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import sys
import uuid
from typing import Optional

from . import TOOL_VERSION
from .db import ConnectionError_, redact_url, resolve_catalog_url, resolve_core_url
from .results import RunReport, render_text
from .runner import expand_check_ids, run_catalog

CATALOG_CHOICES = ["vms", "dayshift", "complete", "esnf", "all"]
ALL_CATALOG_CODES = ["vms", "dayshift", "complete", "esnf"]


def _parse_date(value: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}, expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.integrity_check",
        description="Independent, read-only data-integrity checker for Brink's Nexus.",
    )
    p.add_argument("--catalog", required=True, choices=CATALOG_CHOICES)
    p.add_argument(
        "--catalog-database-url",
        default=None,
        help="Required per-catalog URL. Omit to fall back to DATABASE_URL_<CATALOG> "
        "env var / the app's own settings default (not valid with --catalog all).",
    )
    p.add_argument(
        "--core-database-url",
        default=None,
        help="Required for the cross-database user checks (PG-13). Omit to fall back "
        "to DATABASE_URL_CORE env var / the app's own settings default.",
    )
    p.add_argument("--business-date-from", type=_parse_date, default=None)
    p.add_argument("--business-date-to", type=_parse_date, default=None)
    p.add_argument("--checks", default="all")
    p.add_argument(
        "--severity-threshold", choices=["critical", "high", "medium", "low"], default="low"
    )
    p.add_argument("--sample-size", type=int, default=20)
    p.add_argument("--format", choices=["json", "text"], default=None)
    p.add_argument("--output-file", default=None)
    p.add_argument(
        "--redact-connection-strings",
        dest="redact",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return p


async def _run(args) -> RunReport:
    run_id = str(uuid.uuid4())
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    try:
        check_ids = expand_check_ids(args.checks)
    except ValueError as exc:
        finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return RunReport(
            tool_version=TOOL_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            catalogs=[],
            exit_code=2,
            error=f"bad arguments: {exc}",
        )

    if args.sample_size < 0:
        finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return RunReport(
            tool_version=TOOL_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            catalogs=[],
            exit_code=2,
            error="bad arguments: --sample-size must be >= 0",
        )

    codes = ALL_CATALOG_CODES if args.catalog == "all" else [args.catalog]

    if args.catalog == "all" and args.catalog_database_url:
        finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return RunReport(
            tool_version=TOOL_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            catalogs=[],
            exit_code=2,
            error="bad arguments: --catalog-database-url cannot be used with --catalog all "
            "(each catalog is a separate physical database) -- rely on DATABASE_URL_<CATALOG> "
            "env vars / settings defaults instead",
        )

    core_url = resolve_core_url(args.core_database_url)
    core_url_redacted = redact_url(core_url, redact=args.redact)

    try:
        catalog_reports = []
        for code in codes:
            catalog_url = resolve_catalog_url(args.catalog_database_url, code)
            catalog_url_redacted = redact_url(catalog_url, redact=args.redact)
            report = await run_catalog(
                catalog_code=code,
                catalog_url=catalog_url,
                catalog_url_redacted=catalog_url_redacted,
                core_url=core_url,
                core_url_redacted=core_url_redacted,
                check_ids=check_ids,
                severity_threshold=args.severity_threshold,
                date_from=args.business_date_from,
                date_to=args.business_date_to,
                sample_size=args.sample_size,
            )
            catalog_reports.append(report)
    except ConnectionError_ as exc:
        finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return RunReport(
            tool_version=TOOL_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            catalogs=[],
            exit_code=2,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any unexpected
        # failure running a query is a tool error (exit 2), never mistaken
        # for exit 1 ("found a data problem") or an unhandled traceback.
        finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return RunReport(
            tool_version=TOOL_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            catalogs=[],
            exit_code=2,
            error=f"{type(exc).__name__}: {exc}",
        )

    finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    exit_code = 1 if any(c.exit_code == 1 for c in catalog_reports) else 0
    return RunReport(
        tool_version=TOOL_VERSION,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        catalogs=catalog_reports,
        exit_code=exit_code,
    )


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    fmt = args.format or ("json" if args.output_file else "text")

    report = asyncio.run(_run(args))

    if fmt == "json":
        output = report.to_json()
    else:
        output = render_text(report)

    if args.output_file:
        with open(args.output_file, "w") as f:
            f.write(output)
            f.write("\n")
    else:
        print(output)

    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
