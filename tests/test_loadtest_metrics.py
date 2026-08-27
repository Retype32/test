"""Unit tests for tools/loadtest/metrics.py -- the harness's own outcome
classification and aggregation logic, tested in isolation against
synthetic responses. No live server, no database, no httpx: these tests
exercise exactly the "does the business-success/valid-rejection/unexpected-
failure classifier work correctly" question the Phase 3 task calls out,
independent of the harness's separately-runnable live-server mode.
"""
import httpx
import pytest

from tools.loadtest.metrics import (
    ClassifiedOutcome,
    MetricsCollector,
    Outcome,
    RequestRecord,
    RootCause,
    classify_exception,
    classify_http,
    latency_summary,
    median_iqr,
    normalize_path,
    percentile,
)


# ---------------------------------------------------------------------
# classify_http -- status-code-driven classification
# ---------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201])
def test_expected_success_status_is_business_success(status):
    result = classify_http(status, success_statuses=(200, 201))
    assert result == ClassifiedOutcome(Outcome.BUSINESS_SUCCESS, RootCause.NONE)


def test_redirect_success_status_is_business_success():
    # Wizard POST steps expect 303, not 2xx.
    result = classify_http(303, success_statuses=(303,))
    assert result.bucket == Outcome.BUSINESS_SUCCESS


@pytest.mark.parametrize("status,expected_cause", [
    (400, RootCause.VALIDATION_ERROR),
    (401, RootCause.AUTH_FAILURE),
    (403, RootCause.AUTH_FAILURE),
    (404, RootCause.NOT_FOUND),
    (409, RootCause.DAY_CLOSED_CONFLICT),
    (422, RootCause.VALIDATION_ERROR),
])
def test_known_4xx_statuses_are_valid_rejection_with_named_root_cause(status, expected_cause):
    result = classify_http(status, success_statuses=(200, 201))
    assert result.bucket == Outcome.VALID_REJECTION
    assert result.root_cause == expected_cause


def test_root_cause_hint_overrides_default_mapping():
    # e.g. a 409 caller who knows it's actually a stale-version conflict,
    # not the default day-closed-conflict guess.
    result = classify_http(409, success_statuses=(200,), root_cause_hint=RootCause.OTHER)
    assert result == ClassifiedOutcome(Outcome.VALID_REJECTION, RootCause.OTHER)


def test_unhandled_5xx_is_unexpected_failure():
    result = classify_http(500, success_statuses=(200,))
    assert result == ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.UNHANDLED_5XX)


def test_500_with_pool_exhaustion_signature_is_classified_distinctly():
    result = classify_http(
        500, success_statuses=(200,),
        body_text="sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached",
    )
    assert result == ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.DB_POOL_EXHAUSTION)


def test_503_without_pool_signature_is_generic_unhandled_5xx():
    result = classify_http(503, success_statuses=(200,), body_text="Service temporarily unavailable")
    assert result.bucket == Outcome.UNEXPECTED_FAILURE
    assert result.root_cause == RootCause.UNHANDLED_5XX


def test_unexpected_status_outside_success_and_4xx_5xx_is_unexpected_failure():
    # e.g. a 405 Method Not Allowed nobody's success_statuses ever names.
    result = classify_http(405, success_statuses=(200, 201))
    assert result.bucket == Outcome.UNEXPECTED_FAILURE
    assert result.root_cause == RootCause.OTHER


def test_2xx_not_in_expected_success_statuses_is_not_silently_accepted():
    # A call site that expects 303 (a redirect) but got 200 instead is a
    # deviation from the documented contract, not a free pass to
    # business_success.
    result = classify_http(200, success_statuses=(303,))
    assert result.bucket == Outcome.UNEXPECTED_FAILURE


def test_no_response_status_without_exception_is_unexpected_failure():
    result = classify_http(None, success_statuses=(200,))
    assert result.bucket == Outcome.UNEXPECTED_FAILURE


# ---------------------------------------------------------------------
# classify_exception / classify_http with exc -- transport failures
# ---------------------------------------------------------------------

def test_connect_error_is_connection_refused():
    exc = httpx.ConnectError("Connection refused")
    result = classify_exception(exc)
    assert result == ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.CONNECTION_REFUSED)


def test_timeout_exception_is_client_timeout():
    exc = httpx.ReadTimeout("timed out")
    result = classify_exception(exc)
    assert result == ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.CLIENT_TIMEOUT)


def test_classify_http_with_exception_ignores_status_code():
    # Even if a status_code were somehow also passed, a transport
    # exception always wins -- the request never really completed.
    result = classify_http(200, success_statuses=(200,), exc=httpx.ConnectError("refused"))
    assert result.bucket == Outcome.UNEXPECTED_FAILURE
    assert result.root_cause == RootCause.CONNECTION_REFUSED


def test_unrecognised_exception_falls_back_to_other():
    class WeirdTransportBug(Exception):
        pass

    result = classify_exception(WeirdTransportBug("something else broke"))
    assert result == ClassifiedOutcome(Outcome.UNEXPECTED_FAILURE, RootCause.OTHER)


# ---------------------------------------------------------------------
# normalize_path
# ---------------------------------------------------------------------

def test_normalize_path_collapses_uuid_segment():
    path = "/web/transactions/3c1c9c1a-4b1e-4a2a-9a1a-1a2b3c4d5e6f"
    assert normalize_path(path) == "/web/transactions/{id}"


def test_normalize_path_collapses_uuid_in_middle_of_path():
    path = "/web/duplicates/3c1c9c1a-4b1e-4a2a-9a1a-1a2b3c4d5e6f/review"
    assert normalize_path(path) == "/web/duplicates/{id}/review"


def test_normalize_path_leaves_static_paths_untouched():
    assert normalize_path("/api/v1/transactions/") == "/api/v1/transactions/"
    assert normalize_path("/web/login") == "/web/login"


# ---------------------------------------------------------------------
# percentile / latency_summary
# ---------------------------------------------------------------------

def test_percentile_of_empty_list_is_zero():
    assert percentile([], 95) == 0.0


def test_percentile_of_single_value():
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 99) == 42.0


def test_percentile_p50_matches_median_for_odd_length():
    assert percentile([1, 2, 3, 4, 5], 50) == 3.0


def test_percentile_p100_like_behaviour_at_p99_is_near_max():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 99) == pytest.approx(99.01, abs=0.5)


def test_latency_summary_reports_max_and_count():
    values = [10.0, 20.0, 30.0, 1000.0]
    summary = latency_summary(values)
    assert summary["max"] == 1000.0
    assert summary["count"] == 4
    assert summary["p50"] <= summary["p90"] <= summary["p95"] <= summary["p99"] <= summary["max"]


def test_latency_summary_of_no_requests_is_well_formed_zero():
    summary = latency_summary([])
    assert summary == {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "count": 0}


# ---------------------------------------------------------------------
# MetricsCollector aggregation
# ---------------------------------------------------------------------

def _record(method, path, status, latency_ms, outcome, root_cause=RootCause.NONE):
    return RequestRecord(
        method=method, path=path, status_code=status, latency_ms=latency_ms,
        started_at=0.0, outcome=outcome, root_cause=root_cause,
    )


def test_collector_counts_by_outcome():
    c = MetricsCollector()
    c.append(_record("GET", "/web/login", 200, 5.0, Outcome.BUSINESS_SUCCESS))
    c.append(_record("POST", "/web/login", 401, 6.0, Outcome.VALID_REJECTION, RootCause.AUTH_FAILURE))
    c.append(_record("POST", "/api/v1/transactions/", 500, 7.0, Outcome.UNEXPECTED_FAILURE, RootCause.UNHANDLED_5XX))

    counts = c.counts_by_outcome()
    assert counts == {"business_success": 1, "valid_rejection": 1, "unexpected_failure": 1}


def test_collector_by_status_code_groups_no_response_separately():
    c = MetricsCollector()
    c.append(_record("GET", "/web/login", 200, 5.0, Outcome.BUSINESS_SUCCESS))
    c.append(RequestRecord(
        method="GET", path="/web/login", status_code=None, latency_ms=1.0,
        started_at=0.0, outcome=Outcome.UNEXPECTED_FAILURE, root_cause=RootCause.CONNECTION_REFUSED,
    ))
    by_status = c.by_status_code()
    assert by_status["200"] == 1
    assert by_status["no_response"] == 1


def test_collector_latency_by_endpoint_groups_method_and_path():
    c = MetricsCollector()
    c.append(_record("GET", "/web/login", 200, 10.0, Outcome.BUSINESS_SUCCESS))
    c.append(_record("GET", "/web/login", 200, 20.0, Outcome.BUSINESS_SUCCESS))
    c.append(_record("POST", "/web/login", 303, 15.0, Outcome.BUSINESS_SUCCESS))

    by_endpoint = c.latency_by_endpoint()
    assert set(by_endpoint.keys()) == {"GET /web/login", "POST /web/login"}
    assert by_endpoint["GET /web/login"]["count"] == 2
    assert by_endpoint["POST /web/login"]["count"] == 1


def test_collector_errors_by_endpoint_and_cause_excludes_business_success():
    c = MetricsCollector()
    c.append(_record("GET", "/web/login", 200, 5.0, Outcome.BUSINESS_SUCCESS))
    c.append(_record("POST", "/web/eod/close", 409, 8.0, Outcome.VALID_REJECTION, RootCause.DAY_CLOSED_CONFLICT))
    c.append(_record("POST", "/web/eod/close", 409, 9.0, Outcome.VALID_REJECTION, RootCause.DAY_CLOSED_CONFLICT))

    errors = c.errors_by_endpoint_and_cause()
    assert len(errors) == 1
    assert errors[0]["count"] == 2
    assert errors[0]["root_cause"] == "day_closed_conflict"
    assert errors[0]["endpoint"] == "POST /web/eod/close"


def test_collector_errors_sorted_largest_bucket_first():
    c = MetricsCollector()
    for _ in range(3):
        c.append(_record("GET", "/a", 404, 1.0, Outcome.VALID_REJECTION, RootCause.NOT_FOUND))
    for _ in range(5):
        c.append(_record("GET", "/b", 500, 1.0, Outcome.UNEXPECTED_FAILURE, RootCause.UNHANDLED_5XX))

    errors = c.errors_by_endpoint_and_cause()
    assert errors[0]["count"] == 5
    assert errors[1]["count"] == 3


def test_collector_rps_and_tps():
    c = MetricsCollector()
    for _ in range(20):
        c.append(_record("GET", "/x", 200, 1.0, Outcome.BUSINESS_SUCCESS))
    c.claimed_transaction_ids.extend(["t1", "t2"])

    assert c.rps(10.0) == 2.0
    assert c.tps(10.0) == 0.2


def test_collector_rps_handles_zero_duration_without_dividing_by_zero():
    c = MetricsCollector()
    c.append(_record("GET", "/x", 200, 1.0, Outcome.BUSINESS_SUCCESS))
    assert c.rps(0.0) == 0.0
    assert c.tps(0.0) == 0.0


def test_collector_merge_combines_records_and_claimed_ids():
    a = MetricsCollector()
    a.append(_record("GET", "/x", 200, 1.0, Outcome.BUSINESS_SUCCESS))
    a.claimed_transaction_ids.append("t1")

    b = MetricsCollector()
    b.append(_record("GET", "/y", 200, 1.0, Outcome.BUSINESS_SUCCESS))
    b.claimed_transaction_ids.append("t2")

    a.merge(b)
    assert len(a.records) == 2
    assert a.claimed_transaction_ids == ["t1", "t2"]


# ---------------------------------------------------------------------
# median_iqr -- Profile A's cross-rep aggregation
# ---------------------------------------------------------------------

def test_median_iqr_of_empty_list():
    assert median_iqr([]) == {"median": 0.0, "iqr": 0.0, "q1": 0.0, "q3": 0.0, "n": 0}


def test_median_iqr_basic():
    result = median_iqr([10.0, 20.0, 30.0, 40.0])
    assert result["n"] == 4
    assert result["median"] == pytest.approx(25.0)
    assert result["iqr"] >= 0


def test_median_iqr_single_value_has_zero_spread():
    result = median_iqr([42.0])
    assert result["median"] == 42.0
    assert result["iqr"] == 0.0
