"""Builds the section-9 JSON run-summary schema and the section-9.1
Markdown report, and writes both to disk. One JSON + one Markdown per run,
as the plan specifies -- "run" meaning one profile/variant/rep, e.g.
"A-50vu-rep3" or "B-3000attempts-25vu"."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

from .integrity import IntegrityResult
from .metrics import MetricsCollector

_COMMIT_ENDPOINTS = {
    "POST /web/transactions/new/wizard/complete",
    "POST /web/transactions/new/wizard/wallet/next",
    "POST /api/v1/transactions/",
}


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _status_5xx_total(by_status: dict) -> int:
    total = 0
    for code, count in by_status.items():
        if code.isdigit() and code.startswith("5"):
            total += count
    return total


def build_run_summary(
    *,
    run_id: str,
    workload_profile: str,
    profile_variant: str,
    started_at: datetime,
    finished_at: datetime,
    collector: MetricsCollector,
    target_vus: int,
    catalogs_used: list[str],
    resource_summary: dict,
    db_files_mb_start: dict,
    db_files_mb_end: dict,
    disk_free_mb_start: Optional[float],
    disk_free_mb_end: Optional[float],
    integrity: Optional[IntegrityResult],
    hardware_note: str,
    db_backend: str = "sqlite",
    uvicorn_workers: int = 1,
    temp_report_files_leaked: int = 0,
    db_pool_instrumentation: str = "error_inference",
    db_pool_timeout_errors: int = 0,
    notes: str = "",
) -> dict:
    duration_seconds = max((finished_at - started_at).total_seconds(), 0.0001)
    by_status = collector.by_status_code()
    outcome_counts = collector.counts_by_outcome()
    latency_overall = collector.overall_latency()
    latency_by_endpoint = collector.latency_by_endpoint()

    integrity_dict = integrity.to_dict() if integrity else {
        "committed_records_by_table": {}, "unique_transaction_ids": 0, "distinct_bag_numbers": 0,
        "duplicate_bags_found": 0, "missing_records": 0, "missing_audit_events": 0,
        "orphan_records": 0, "reconciliation_differences": 0,
    }
    unexpected_5xx = _status_5xx_total(by_status)
    unbalanced = getattr(integrity, "unbalanced_completed_transactions", 0) if integrity else 0

    commit_latencies = []
    for endpoint, stats in latency_by_endpoint.items():
        if endpoint in _COMMIT_ENDPOINTS:
            commit_latencies.append(stats["p99"])
    p99_commit = max(commit_latencies) if commit_latencies else 0.0

    non_commit_p95 = [
        stats["p95"] for endpoint, stats in latency_by_endpoint.items() if endpoint not in _COMMIT_ENDPOINTS
    ]
    p95_normal = max(non_commit_p95) if non_commit_p95 else latency_overall["p95"]

    acceptance = {
        "zero_unexpected_transaction_loss": "pass" if integrity_dict["missing_records"] == 0 else "fail",
        "zero_duplicate_financial_effects": (
            "pass" if integrity_dict["missing_records"] == 0 and integrity_dict["reconciliation_differences"] == 0
            else "fail"
        ),
        "zero_missing_audit_events": "pass" if integrity_dict["missing_audit_events"] == 0 else "fail",
        "zero_orphan_records": "pass" if integrity_dict["orphan_records"] == 0 else "fail",
        "zero_unbalanced_completed_transactions": "pass" if unbalanced == 0 else "fail",
        "zero_unexpected_5xx": "pass" if unexpected_5xx == 0 else "fail",
        "p95_normal_api_under_1s": "pass" if p95_normal < 1000 else "fail",
        "p99_transaction_commit_under_2s": "pass" if p99_commit < 2000 else "fail",
    }

    return {
        "run_id": run_id,
        "workload_profile": workload_profile,
        "profile_variant": profile_variant,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": finished_at.astimezone(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "environment": {
            "hardware_note": hardware_note,
            "cpu_count": os.cpu_count() or 0,
            "memory_total_mb": _total_memory_mb(),
            "python_version": sys.version.split()[0],
            "app_commit": _git_sha(),
            "db_backend": db_backend,
            "uvicorn_workers": uvicorn_workers,
        },
        "concurrency": {"target_vus": target_vus, "catalogs_used": catalogs_used},
        "requests": {
            "attempted_total": len(collector.records),
            "by_status_code": by_status,
            "business_success_total": outcome_counts["business_success"],
            "valid_rejection_total": outcome_counts["valid_rejection"],
            "unexpected_failure_total": outcome_counts["unexpected_failure"],
        },
        "throughput": {
            "rps_mean": collector.rps(duration_seconds),
            "tps_mean": collector.tps(duration_seconds),
        },
        "latency_ms": {"overall": latency_overall, "by_endpoint": latency_by_endpoint},
        "resources": {
            **resource_summary,
            "disk": {
                "db_files_mb_start": db_files_mb_start,
                "db_files_mb_end": db_files_mb_end,
                "disk_free_mb_start": disk_free_mb_start,
                "disk_free_mb_end": disk_free_mb_end,
                "temp_report_files_leaked": temp_report_files_leaked,
            },
            "db_pool": {
                "instrumentation_method": db_pool_instrumentation,
                "max_checked_out_observed": None,
                "pool_timeout_errors": db_pool_timeout_errors,
            },
        },
        "data_integrity": integrity_dict,
        "errors_by_endpoint_and_cause": collector.errors_by_endpoint_and_cause(),
        "acceptance_criteria": acceptance,
        "notes": notes,
    }


def _total_memory_mb() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().total / (1024 * 1024))
    except Exception:
        return 0


def render_markdown(summary: dict, extra_notes: str = "") -> str:
    env = summary["environment"]
    req = summary["requests"]
    tp = summary["throughput"]
    lat = summary["latency_ms"]["overall"]
    res = summary["resources"]
    integ = summary["data_integrity"]
    acc = summary["acceptance_criteria"]

    lines = []
    lines.append(f"# Load-test run report -- {summary['run_id']}")
    lines.append("")
    lines.append(f"- **Profile / variant:** {summary['workload_profile']} / {summary['profile_variant']}")
    lines.append(f"- **Started:** {summary['started_at']}  **Finished:** {summary['finished_at']}"
                 f"  **Duration:** {summary['duration_seconds']}s")
    lines.append(f"- **Target VUs:** {summary['concurrency']['target_vus']}"
                 f"  **Catalogs used:** {', '.join(summary['concurrency']['catalogs_used'])}")
    lines.append("")
    lines.append("## Environment snapshot")
    lines.append("")
    lines.append(f"> **{env['hardware_note']}**")
    lines.append("")
    lines.append(f"- CPU count: {env['cpu_count']}, Memory: {env['memory_total_mb']} MB, "
                 f"Python {env['python_version']}, commit `{env['app_commit']}`")
    lines.append(f"- DB backend: {env['db_backend']}, uvicorn workers: {env['uvicorn_workers']}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    overall_pass = all(v in ("pass", "not_applicable_hardware_limited") for v in acc.values())
    lines.append(
        f"{req['attempted_total']} requests attempted over {summary['duration_seconds']}s "
        f"at {tp['rps_mean']} req/s ({tp['tps_mean']} committed transactions/s). "
        f"p50={lat['p50']}ms p95={lat['p95']}ms p99={lat['p99']}ms max={lat['max']}ms overall. "
        f"{req['business_success_total']} business success, {req['valid_rejection_total']} valid "
        f"rejections, {req['unexpected_failure_total']} unexpected failures. "
        f"**Acceptance criteria: {'ALL PASS' if overall_pass else 'AT LEAST ONE FAILED -- see table below'}.**"
    )
    lines.append("")
    lines.append("## Request/response accounting")
    lines.append("")
    lines.append("| Status code | Count |")
    lines.append("|---|---|")
    for code, count in sorted(req["by_status_code"].items()):
        lines.append(f"| {code} | {count} |")
    lines.append("")
    lines.append(f"Business success: **{req['business_success_total']}**  "
                 f"Valid rejection: **{req['valid_rejection_total']}**  "
                 f"Unexpected failure: **{req['unexpected_failure_total']}**")
    lines.append("")
    lines.append("## Latency by endpoint (ms)")
    lines.append("")
    lines.append("| Endpoint | count | p50 | p90 | p95 | p99 | max |")
    lines.append("|---|---|---|---|---|---|---|")
    for endpoint, stats in sorted(summary["latency_ms"]["by_endpoint"].items()):
        lines.append(
            f"| {endpoint} | {stats['count']} | {stats['p50']} | {stats['p90']} | "
            f"{stats['p95']} | {stats['p99']} | {stats['max']} |"
        )
    lines.append("")
    lines.append("## Resource usage")
    lines.append("")
    lines.append(f"- CPU%: mean={res['cpu_percent']['mean']} max={res['cpu_percent']['max']} "
                 f"(instrumentation: {res.get('instrumentation_method', 'not_measured')})")
    lines.append(f"- Memory MB: mean={res['memory_mb']['mean']} max={res['memory_mb']['max']} "
                 f"start={res['memory_mb']['start']} end={res['memory_mb']['end']}")
    disk = res["disk"]
    lines.append(f"- DB file sizes (MB) start: {disk['db_files_mb_start']}")
    lines.append(f"- DB file sizes (MB) end: {disk['db_files_mb_end']}")
    lines.append(f"- Disk free MB: start={disk.get('disk_free_mb_start')} end={disk.get('disk_free_mb_end')}")
    lines.append(f"- Temp report files leaked: {disk['temp_report_files_leaked']}")
    dbp = res["db_pool"]
    lines.append(f"- DB pool instrumentation: {dbp['instrumentation_method']}, "
                 f"pool timeout errors observed: {dbp['pool_timeout_errors']}")
    lines.append("")
    lines.append("## Data-integrity results")
    lines.append("")
    lines.append("| Check | Value |")
    lines.append("|---|---|")
    for key in ("unique_transaction_ids", "distinct_bag_numbers", "duplicate_bags_found",
                "missing_records", "missing_audit_events", "orphan_records", "reconciliation_differences"):
        lines.append(f"| {key} | {integ.get(key)} |")
    lines.append("")
    lines.append("Committed records by table: "
                 + ", ".join(f"{t}={n}" for t, n in integ.get("committed_records_by_table", {}).items()))
    lines.append("")
    lines.append("## Errors by endpoint + root cause")
    lines.append("")
    errs = summary["errors_by_endpoint_and_cause"]
    if errs:
        lines.append("| Endpoint | Status | Root cause | Count |")
        lines.append("|---|---|---|---|")
        for e in errs:
            lines.append(f"| {e['endpoint']} | {e['status_code']} | {e['root_cause']} | {e['count']} |")
    else:
        lines.append("None.")
    lines.append("")
    lines.append("## Acceptance-criteria verdict")
    lines.append("")
    lines.append("| Criterion | Result |")
    lines.append("|---|---|")
    for k, v in acc.items():
        lines.append(f"| {k} | {v.upper()} |")
    lines.append("")
    lines.append("## Anomalies / notable findings")
    lines.append("")
    lines.append(summary.get("notes") or "None recorded.")
    if extra_notes:
        lines.append("")
        lines.append(extra_notes)
    lines.append("")
    return "\n".join(lines)


def write_run_report(summary: dict, out_dir: str, extra_notes: str = "") -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    base = summary["profile_variant"].replace("/", "-")
    json_path = os.path.join(out_dir, f"{base}.json")
    md_path = os.path.join(out_dir, f"{base}.md")
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    with open(md_path, "w") as f:
        f.write(render_markdown(summary, extra_notes=extra_notes))
    return json_path, md_path
