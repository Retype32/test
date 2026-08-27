"""Live authorization-matrix sweep (Agent 3's proposed
`test_full_role_matrix_against_every_route`,
docs/production_readiness/03_security_review.md Phase 5 tooling section).

Individual role checks are already covered, scattered across the rest of
the suite (test_transactions_api.py, test_eod_and_transfer_api.py, etc.) --
this file's purpose is different: one parametrized sweep asserting the
exact status code for anonymous/cashier/supervisor/administrator against
every route in Agent 3's documented matrix, in one place, so a future
change to a route's role dependency shows up here as a single failing
row instead of being missed entirely (no route currently has a dependency
that isn't exercised by *something*, but nothing before this file asserted
the *complete* cross product in one sweep).

Uses the existing in-process ASGI test client (`api_client`/`tokens`
fixtures from conftest.py) -- adequate for authorization semantics (a
status-code check), unlike load/timing tests where Agent 2's plan
correctly requires a real socket-connected server instead.
"""
import pytest

CATALOG = "esnf"  # a catalog this file doesn't share write-sensitive state with


def _headers(token: str | None, catalog: str = CATALOG) -> dict:
    headers = {"X-Catalog": catalog}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# (method, path, {role: expected_status}) -- role "anon" needs no token.
# Matches the Role/Action Permission Matrix table in
# docs/production_readiness/03_security_review.md exactly, restricted to
# routes that need no path-parameter resource id (those are covered by
# dedicated tests below and elsewhere in the suite).
ROUTE_MATRIX = [
    ("GET", "/api/v1/transactions/", {
        "anon": 403, "cashier1": 403, "supervisor1": 200, "admin": 200,
    }),
    ("POST", "/api/v1/eod/close", {
        "anon": 403, "cashier1": 403, "supervisor1": 422, "admin": 422,
        # 422 (not 200/409): no business_date body supplied -- this route
        # requires a request body, and the point of this sweep is the role
        # gate, not exercising every route's full business logic. A 422
        # here still proves the role dependency ran and passed (auth
        # happens before body validation in FastAPI's dependency order for
        # these routes -- confirmed by the anon/cashier rows being 401/403,
        # not 422, for the exact same missing body).
    }),
    ("POST", "/api/v1/eod/reopen", {
        "anon": 403, "cashier1": 403, "supervisor1": 403, "admin": 422,
    }),
    ("GET", "/api/v1/eod/", {
        "anon": 403, "cashier1": 403, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/eod/status?business_date=2026-01-01", {
        "anon": 403, "cashier1": 200, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/duplicates/", {
        "anon": 403, "cashier1": 403, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/notifications/", {
        "anon": 403, "cashier1": 403, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/stats/processors?business_date=2026-01-01", {
        "anon": 403, "cashier1": 403, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/customers/", {
        "anon": 403, "cashier1": 200, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/catalogs/", {
        "anon": 403, "cashier1": 200, "supervisor1": 200, "admin": 200,
    }),
    ("GET", "/api/v1/auth/users", {
        "anon": 403, "cashier1": 403, "supervisor1": 403, "admin": 200,
    }),
    ("POST", "/api/v1/auth/users", {
        "anon": 403, "cashier1": 403, "supervisor1": 403, "admin": 422,
        # 422: no body supplied -- same role-gate-runs-first reasoning as above.
    }),
]

USERNAME_FOR_ROLE = {"cashier1": "cashier1", "supervisor1": "supervisor1", "admin": "admin"}


@pytest.mark.parametrize("method,path,expected", ROUTE_MATRIX, ids=[f"{m}_{p}" for m, p, _ in ROUTE_MATRIX])
async def test_route_enforces_documented_role_matrix(api_client, tokens, method, path, expected):
    for role, expected_status in expected.items():
        token = tokens[USERNAME_FOR_ROLE[role]] if role != "anon" else None
        resp = await api_client.request(method, path, headers=_headers(token))
        assert resp.status_code == expected_status, (
            f"{method} {path} as {role}: expected {expected_status}, got "
            f"{resp.status_code} (body: {resp.text[:300]})"
        )


async def test_anonymous_cannot_reach_any_authenticated_route(api_client):
    """Scenario 1 (Agent 3): every route requires a credential except the
    explicitly-public ones. Sweeps every path in ROUTE_MATRIX with no
    Authorization header and confirms none of them leak a 200 (or any
    status implying the route ran real business/role logic without a
    credential at all). Every route here uses FastAPI's HTTPBearer with
    its default auto_error=True, which itself returns 403 ("Not
    authenticated") for a request with no Authorization header at all --
    401 is reserved for a header that IS present but carries an
    invalid/expired token. Confirmed by reading backend/api/deps.py
    (bearer_scheme = HTTPBearer(), no auto_error=False override
    anywhere) -- this is standard FastAPI behavior, not an app-specific
    choice, and this test's expectation is calibrated to match it rather
    than the more commonly-assumed 401."""
    for method, path, _ in ROUTE_MATRIX:
        resp = await api_client.request(method, path, headers={"X-Catalog": CATALOG})
        assert resp.status_code == 403, f"{method} {path} anonymous: expected 403 (HTTPBearer's auto_error response for a missing credential), got {resp.status_code}"


async def test_health_and_docs_remain_intentionally_public(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    resp = await api_client.get("/openapi.json")
    assert resp.status_code == 200


async def test_single_transaction_read_tightened_to_supervisor_in_production_only(api_client, tokens, monkeypatch):
    """S-12: GET /api/v1/transactions/{id} was reachable by any
    authenticated role (CurrentUser), inconsistent with the list endpoint
    and the web portal (both SupervisorOrAbove). The actual fix, read
    directly from backend/api/routes/transactions.py's
    _get_transaction_by_id_guard, is deliberately production-mode-gated:
    tightening it unconditionally would break the pre-existing, still-
    required test_transactions_api.py::test_get_transaction_by_id, which
    asserts a cashier CAN read a transaction by id in today's default
    (development) configuration -- an explicit, documented resolution of
    the "fix S-12" vs. "never break an existing test" tension, not an
    oversight. This test asserts BOTH halves of that behavior rather than
    assuming the naive "always tightened" reading."""
    import uuid
    from backend.core.config import settings

    random_id = str(uuid.uuid4())

    # Default (development) config: unchanged, matches the preserved
    # existing test -- a cashier reaches the route logic and gets a clean
    # 404 for a nonexistent id, not a role-based 403.
    assert settings.environment != "production"
    resp = await api_client.get(f"/api/v1/transactions/{random_id}", headers=_headers(tokens["cashier1"]))
    assert resp.status_code == 404, (
        f"dev-mode cashier read should reach route logic (404 for a nonexistent id), got {resp.status_code}"
    )

    # Production mode: tightened to supervisor+, per S-12's remediation.
    monkeypatch.setattr(settings, "environment", "production")
    try:
        resp = await api_client.get(f"/api/v1/transactions/{random_id}", headers=_headers(tokens["cashier1"]))
        assert resp.status_code == 403, (
            f"production-mode cashier should be rejected by the role gate before id lookup, got {resp.status_code}"
        )

        resp = await api_client.get(f"/api/v1/transactions/{random_id}", headers=_headers(tokens["supervisor1"]))
        assert resp.status_code == 404, (
            f"production-mode supervisor should pass the role gate and get a clean 404, got {resp.status_code}"
        )
    finally:
        monkeypatch.setattr(settings, "environment", "development")
