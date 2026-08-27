"""Result dataclasses, JSON serialization, and the `--format text` renderer.

Schema follows docs/production_readiness/04_postgresql_and_reconciliation.md
§4.3 field-for-field (tool_version, run_id, timestamps, catalog, redacted
URLs, scope, checks_run, checks_not_applicable, summary, results,
exit_code). A few `results[]` entries carry one additional, additive key
beyond that schema (documented on the specific check) for information that
doesn't fit the pass/fail violation model -- never removing or renaming a
required key, so any consumer reading only the documented fields still gets
exactly what §4.3 promises.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import decimal
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def json_default(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {value!r}")


@dataclass
class CheckOutcome:
    """What a check function returns -- the runner fills in timing/status."""

    violation_count: int
    sample_violations: list
    extra: Optional[dict] = None


@dataclass
class CheckResult:
    check_id: str
    description: str
    severity: str
    status: str  # "pass" | "fail"
    violation_count: int
    query_duration_ms: int
    sample_violations: list
    extra: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "check_id": self.check_id,
            "description": self.description,
            "severity": self.severity,
            "status": self.status,
            "violation_count": self.violation_count,
            "query_duration_ms": self.query_duration_ms,
            "sample_violations": self.sample_violations,
        }
        if self.extra:
            d.update(self.extra)
        return d


@dataclass
class CatalogReport:
    catalog: str
    catalog_database_url_redacted: str
    core_database_url_redacted: Optional[str]
    business_date_from: Optional[str]
    business_date_to: Optional[str]
    rows_scanned: dict
    checks_run: list
    checks_not_applicable: list
    results: list  # list[CheckResult]
    exit_code: int

    def summary(self) -> dict:
        passed = sum(1 for r in self.results if r.status == "pass")
        failed = sum(1 for r in self.results if r.status == "fail")
        total_violations = sum(r.violation_count for r in self.results)
        return {
            "checks_passed": passed,
            "checks_failed": failed,
            "checks_not_applicable": len(self.checks_not_applicable),
            "total_violations": total_violations,
        }

    def to_dict(self) -> dict:
        return {
            "catalog": self.catalog,
            "catalog_database_url_redacted": self.catalog_database_url_redacted,
            "core_database_url_redacted": self.core_database_url_redacted,
            "scope": {
                "business_date_from": self.business_date_from,
                "business_date_to": self.business_date_to,
                "rows_scanned": self.rows_scanned,
            },
            "checks_run": self.checks_run,
            "checks_not_applicable": self.checks_not_applicable,
            "summary": self.summary(),
            "results": [r.to_dict() for r in self.results],
            "exit_code": self.exit_code,
        }


@dataclass
class RunReport:
    tool_version: str
    run_id: str
    started_at: str
    finished_at: str
    catalogs: list  # list[CatalogReport]
    exit_code: int
    error: Optional[str] = None

    def to_dict(self) -> dict:
        base = {
            "tool_version": self.tool_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.error is not None:
            base["error"] = self.error
            base["exit_code"] = self.exit_code
            return base
        if len(self.catalogs) == 1:
            # Single-catalog run: flatten, matching §4.3's example exactly
            # (no extra "catalogs" nesting for the common case).
            base.update(self.catalogs[0].to_dict())
            base["exit_code"] = self.exit_code
            return base
        # --catalog all: one object per catalog, never silently merged,
        # plus a top-level aggregate summary wrapper.
        base["catalog"] = "all"
        base["catalogs"] = {c.catalog: c.to_dict() for c in self.catalogs}
        agg = {"checks_passed": 0, "checks_failed": 0, "checks_not_applicable": 0, "total_violations": 0}
        for c in self.catalogs:
            s = c.summary()
            for k in agg:
                agg[k] += s[k]
        base["summary"] = agg
        base["exit_code"] = self.exit_code
        return base

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=json_default)


def render_text(report: RunReport) -> str:
    lines = []
    lines.append(f"integrity_check v{report.tool_version}  run_id={report.run_id}")
    lines.append(f"started={report.started_at}  finished={report.finished_at}")
    if report.error is not None:
        lines.append("")
        lines.append(f"ERROR: {report.error}")
        lines.append("")
        lines.append(f"exit_code={report.exit_code}")
        return "\n".join(lines)

    for c in report.catalogs:
        lines.append("")
        lines.append("=" * 72)
        lines.append(f"catalog: {c.catalog}")
        lines.append(f"  catalog db: {c.catalog_database_url_redacted}")
        if c.core_database_url_redacted:
            lines.append(f"  core db:    {c.core_database_url_redacted}")
        if c.business_date_from or c.business_date_to:
            lines.append(
                f"  business_date: {c.business_date_from or '(any)'} .. {c.business_date_to or '(any)'}"
            )
        lines.append(f"  rows_scanned: {c.rows_scanned}")
        lines.append("")
        for r in c.results:
            marker = "PASS" if r.status == "pass" else "FAIL"
            lines.append(
                f"  [{marker}] {r.check_id} (severity={r.severity}, "
                f"violations={r.violation_count}, {r.query_duration_ms}ms)"
            )
            lines.append(f"         {r.description}")
            for sv in r.sample_violations[:5]:
                lines.append(f"         - {sv}")
            if r.violation_count > len(r.sample_violations):
                lines.append(
                    f"         ... and {r.violation_count - len(r.sample_violations)} more"
                )
        if c.checks_not_applicable:
            lines.append("")
            lines.append("  not applicable:")
            for na in c.checks_not_applicable:
                lines.append(f"    - {na['check_id']}: {na['reason']}")
        s = c.summary()
        lines.append("")
        lines.append(
            f"  summary: {s['checks_passed']} passed, {s['checks_failed']} failed, "
            f"{s['checks_not_applicable']} not applicable, {s['total_violations']} total violations"
        )

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"exit_code={report.exit_code}")
    return "\n".join(lines)
