"""Outcome classification and metrics aggregation for the load-test harness.

This module is deliberately pure/synchronous and free of any httpx/asyncio
dependency so it can be unit-tested against synthetic responses in isolation
(see tests/test_loadtest_metrics.py) -- no live server or DB required.

Classification follows docs/production_readiness/02_capacity_test_plan.md
section 6: every request is bucketed into exactly one of

    business_success | valid_rejection | unexpected_failure

with a root-cause category attached (section 6's named list: validation
error, day-closed conflict, duplicate-bag soft-flag, auth failure,
DB pool exhaustion/timeout, connection refused, unhandled 5xx, other).

The "2xx but the claimed DB effect never landed" half of business_success's
definition (plan section 6) is intentionally NOT decided here -- that check
needs a real database read after the run completes and is layered on top by
tools/loadtest/integrity.py, which can flip a provisionally-business-success
request's contribution to data_integrity.missing_records without needing to
re-run classify_http per request.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


class Outcome(str, Enum):
    BUSINESS_SUCCESS = "business_success"
    VALID_REJECTION = "valid_rejection"
    UNEXPECTED_FAILURE = "unexpected_failure"


class RootCause(str, Enum):
    NONE = "none"
    VALIDATION_ERROR = "validation_error"
    DAY_CLOSED_CONFLICT = "day_closed_conflict"
    DUPLICATE_BAG_SOFT_FLAG = "duplicate_bag_soft_flag"
    AUTH_FAILURE = "auth_failure"
    NOT_FOUND = "not_found"
    DB_POOL_EXHAUSTION = "db_pool_exhaustion_or_timeout"
    CONNECTION_REFUSED = "connection_refused"
    CLIENT_TIMEOUT = "client_timeout"
    UNHANDLED_5XX = "unhandled_5xx"
    OTHER = "other"


# Root-cause categories the plan (section 6) explicitly names for the
# errors_by_endpoint_and_cause bucketing. Kept as a plain tuple of strings
# (not the Enum) because the JSON schema (section 9) declares root_cause as
# a free string, "from the named category list in section 6".
NAMED_ROOT_CAUSE_CATEGORIES = (
    "validation_error",
    "day_closed_conflict",
    "duplicate_bag_soft_flag",
    "auth_failure",
    "db_pool_exhaustion_or_timeout",
    "connection_refused",
    "unhandled_5xx",
    "other",
)

# Status codes the plan calls out explicitly as "expected 4xx from real
# business rules" (section 6) -- these are valid_rejection, never
# unexpected_failure, regardless of which endpoint produced them.
_VALID_REJECTION_STATUSES = {400, 401, 403, 404, 409, 422}

_DEFAULT_ROOT_CAUSE_BY_STATUS = {
    400: RootCause.VALIDATION_ERROR,
    401: RootCause.AUTH_FAILURE,
    403: RootCause.AUTH_FAILURE,
    404: RootCause.NOT_FOUND,
    409: RootCause.DAY_CLOSED_CONFLICT,
    422: RootCause.VALIDATION_ERROR,
}

_POOL_EXHAUSTION_PATTERN = re.compile(
    r"queuepool|pool.?timeout|timeoutexception|connection.?timed.?out|too many connections",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClassifiedOutcome:
    bucket: Outcome
    root_cause: RootCause


def classify_exception(exc: BaseException) -> ClassifiedOutcome:
    """Classify a transport-level failure (no HTTP response was ever
    received) -- connection refused/reset, DNS failure, client-side
    timeout. Any of these is, by definition, an unexpected_failure
    (plan section 6: "any ... connection reset/refused, client-side
    timeout ... " is unexpected_failure, never a valid rejection)."""
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    if "timeout" in name or "timeout" in text:
        return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.CLIENT_TIMEOUT)
    if "connect" in name or "refused" in text or "reset" in text:
        return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.CONNECTION_REFUSED)
    return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.OTHER)


def classify_http(
    status_code: Optional[int],
    *,
    success_statuses: Iterable[int] = (200, 201),
    exc: Optional[BaseException] = None,
    body_text: str = "",
    root_cause_hint: Optional[RootCause] = None,
) -> ClassifiedOutcome:
    """Classify one HTTP round trip.

    status_code: the response's HTTP status, or None if the request never
        got a response (use `exc` in that case).
    success_statuses: the status code(s) this particular call site expects
        on business success -- e.g. {201} for `POST /api/v1/transactions/`,
        {303} for a wizard step's redirect, {200} for a rendered page GET.
        Anything outside this set is judged against the general 4xx/5xx
        rules below, never silently treated as success.
    exc: a transport exception if the request never completed.
    body_text: response body, sniffed for pool-exhaustion signatures on a
        5xx so it can be distinguished from a generic unhandled 5xx.
    root_cause_hint: lets a caller that knows more about *why* a given 4xx
        happened (e.g. this specific 409 is a day-closed conflict vs. a
        stale-version conflict) override the generic per-status mapping.
    """
    if exc is not None:
        return classify_exception(exc)

    if status_code is None:
        return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.OTHER)

    if status_code in set(success_statuses):
        return ClassifiedOutcome(Outcome.BUSINESS_SUCCESS, RootCause.NONE)

    if status_code in _VALID_REJECTION_STATUSES:
        cause = root_cause_hint or _DEFAULT_ROOT_CAUSE_BY_STATUS.get(status_code, RootCause.OTHER)
        return ClassifiedOutcome(Outcome.VALID_REJECTION, cause)

    if 500 <= status_code < 600:
        if _POOL_EXHAUSTION_PATTERN.search(body_text or ""):
            return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.DB_POOL_EXHAUSTION)
        return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.UNHANDLED_5XX)

    # Anything else (an unexpected 2xx/3xx not in success_statuses, a 405,
    # a 429, ...) is a deviation from the documented contract for this call
    # site and is reported as unexpected_failure/other rather than silently
    # folded into either "real" bucket.
    return ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.OTHER)


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_NUMERIC_ID_RE = re.compile(r"(?<=/)\d+(?=/|$)")


def normalize_path(path: str) -> str:
    """Collapse dynamic path segments (transaction UUIDs, flag/notification
    UUIDs, numeric IDs) into a placeholder so latency_ms.by_endpoint (plan
    section 9) groups by route shape, not by individual resource -- a blended
    per-URL breakdown with one row per transaction_id would defeat the
    "broken out per endpoint" requirement instead of satisfying it."""
    path = _UUID_RE.sub("{id}", path)
    path = _NUMERIC_ID_RE.sub("{id}", path)
    return path


@dataclass
class RequestRecord:
    method: str
    path: str  # normalized, e.g. "POST /api/v1/transactions/"
    status_code: Optional[int]
    latency_ms: float
    started_at: float  # epoch seconds
    outcome: Outcome
    root_cause: RootCause
    sample_detail: str = ""


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (0-100), matching the common
    "nearest-rank with interpolation" definition used by most load-test
    tools. Returns 0.0 for an empty input rather than raising, since a
    profile/endpoint that received zero requests is a legitimate (if
    unusual) state to report, not an error."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    d0 = ordered[f] * (c - k)
    d1 = ordered[c] * (k - f)
    return float(d0 + d1)


def latency_summary(values: list[float]) -> dict:
    return {
        "p50": round(percentile(values, 50), 2),
        "p90": round(percentile(values, 90), 2),
        "p95": round(percentile(values, 95), 2),
        "p99": round(percentile(values, 99), 2),
        "max": round(max(values), 2) if values else 0.0,
        "count": len(values),
    }


@dataclass
class MetricsCollector:
    """Accumulates RequestRecords for one run and produces the aggregates
    the section-9 JSON schema needs. Thread-unsafe by design (the harness
    is asyncio, single-threaded event loop; append() is called directly
    from each virtual user's coroutine, no lock needed)."""

    records: list[RequestRecord] = field(default_factory=list)
    # Transaction-create attempts whose response claimed a transaction_id
    # was committed (2xx create/complete). Populated by journeys.py,
    # consumed by integrity.py's missing_records check.
    claimed_transaction_ids: list[str] = field(default_factory=list)

    def append(self, record: RequestRecord) -> None:
        self.records.append(record)

    # -- aggregate helpers -------------------------------------------------

    def by_status_code(self) -> dict:
        counts: dict[str, int] = {}
        for r in self.records:
            key = str(r.status_code) if r.status_code is not None else "no_response"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def counts_by_outcome(self) -> dict:
        out = {o.value: 0 for o in Outcome}
        for r in self.records:
            out[r.outcome.value] += 1
        return out

    def overall_latency(self) -> dict:
        return latency_summary([r.latency_ms for r in self.records])

    def latency_by_endpoint(self) -> dict:
        grouped: dict[str, list[float]] = {}
        for r in self.records:
            key = f"{r.method} {r.path}"
            grouped.setdefault(key, []).append(r.latency_ms)
        return {k: latency_summary(v) for k, v in grouped.items()}

    def errors_by_endpoint_and_cause(self) -> list[dict]:
        buckets: dict[tuple, dict] = {}
        for r in self.records:
            if r.outcome == Outcome.BUSINESS_SUCCESS:
                continue
            key = (f"{r.method} {r.path}", r.status_code, r.root_cause.value)
            if key not in buckets:
                buckets[key] = {
                    "endpoint": key[0],
                    "status_code": key[1],
                    "root_cause": key[2],
                    "count": 0,
                    "sample_detail": r.sample_detail,
                }
            buckets[key]["count"] += 1
        return sorted(buckets.values(), key=lambda b: b["count"], reverse=True)

    def rps(self, duration_seconds: float) -> float:
        if duration_seconds <= 0:
            return 0.0
        return round(len(self.records) / duration_seconds, 3)

    def tps(self, duration_seconds: float) -> float:
        """Business-successful transaction-creates per second -- distinct
        from RPS (plan section 6). Counted by claimed_transaction_ids, not
        by matching an endpoint name, so both the web wizard's
        POST .../complete and the API's POST /transactions/ contribute the
        same way."""
        if duration_seconds <= 0:
            return 0.0
        return round(len(self.claimed_transaction_ids) / duration_seconds, 3)

    def merge(self, other: "MetricsCollector") -> None:
        self.records.extend(other.records)
        self.claimed_transaction_ids.extend(other.claimed_transaction_ids)


def median_iqr(values: list[float]) -> dict:
    """Median + interquartile range across N repetitions of the same tier
    (plan section 5, Profile A: "report median + IQR across the remaining
    4" reps)."""
    if not values:
        return {"median": 0.0, "iqr": 0.0, "q1": 0.0, "q3": 0.0, "n": 0}
    ordered = sorted(values)
    return {
        "median": round(statistics.median(ordered), 3),
        "q1": round(percentile(ordered, 25), 3),
        "q3": round(percentile(ordered, 75), 3),
        "iqr": round(percentile(ordered, 75) - percentile(ordered, 25), 3),
        "n": len(ordered),
    }
