"""Transaction create/list/get and balance-status calculation.

Uses the 'complete' catalog exclusively for transaction writes so it never
interferes with the EOD/transfer tests (dayshift) or the stats tests (esnf),
which depend on exact counts/sums within their own catalog.
"""
import uuid
from decimal import Decimal

CATALOG = "complete"


def _headers(token: str, catalog: str = CATALOG) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": catalog}


def _bag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _payload(bag_number: str, customer_id="C001", location_id="L001", total="180.00", expected_total=None):
    denominations = [
        {"denomination": "€100", "count": 1, "value": "100.00"},
        {"denomination": "€50", "count": 1, "value": "50.00"},
        {"denomination": "€20", "count": 1, "value": "20.00"},
    ]
    payload = {
        "customer_id": customer_id,
        "location_id": location_id,
        "bag_number": bag_number,
        "total_value": total,
        "denominations": denominations,
    }
    if expected_total is not None:
        payload["expected_total"] = expected_total
    return payload


async def test_create_balanced_transaction(api_client, tokens):
    # balance_status reflects whether the counted total (total_value) matches
    # what was expected for the bag (expected_total), not whether total_value
    # merely agrees with its own denomination breakdown.
    payload = _payload(_bag("BAL"), total="170.00", expected_total="170.00")
    r = await api_client.post("/api/v1/transactions/", json=payload, headers=_headers(tokens["cashier1"]))
    assert r.status_code == 201
    body = r.json()
    assert body["balance_status"] == "BALANCED"
    assert body["username"] == "cashier1"
    assert Decimal(body["total_value"]) == Decimal("170.00")
    assert len(body["denominations"]) == 3


async def test_create_transaction_actual_total_mismatch_is_not_balanced(api_client, tokens):
    payload = _payload(_bag("UNBAL"), total="170.00", expected_total="999.00")
    r = await api_client.post("/api/v1/transactions/", json=payload, headers=_headers(tokens["cashier1"]))
    assert r.status_code == 201
    assert r.json()["balance_status"] == "NOT BALANCED"


async def test_create_transaction_without_expected_total_is_pending(api_client, tokens):
    payload = _payload(_bag("NOEXP"), total="170.00")
    r = await api_client.post("/api/v1/transactions/", json=payload, headers=_headers(tokens["cashier1"]))
    assert r.status_code == 201
    assert r.json()["balance_status"] == "PENDING"


async def test_create_transaction_unknown_customer_rejected(api_client, tokens):
    payload = _payload(_bag("BADCUST"), customer_id="NOPE", total="170.00")
    r = await api_client.post("/api/v1/transactions/", json=payload, headers=_headers(tokens["cashier1"]))
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


async def test_create_transaction_location_not_belonging_to_customer_rejected(api_client, tokens):
    payload = _payload(_bag("MISMATCH"), customer_id="C001", location_id="L010", total="170.00")
    r = await api_client.post("/api/v1/transactions/", json=payload, headers=_headers(tokens["cashier1"]))
    assert r.status_code == 400
    assert "does not belong" in r.json()["detail"]


async def test_get_transaction_by_id(api_client, tokens):
    bag = _bag("GETME")
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(bag, total="170.00"), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    r = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["cashier1"]))
    assert r.status_code == 200
    assert r.json()["bag_number"] == bag


async def test_get_nonexistent_transaction_404(api_client, tokens):
    r = await api_client.get(f"/api/v1/transactions/{uuid.uuid4()}", headers=_headers(tokens["admin"]))
    assert r.status_code == 404


async def test_cashier_cannot_list_transactions(api_client, tokens):
    r = await api_client.get("/api/v1/transactions/", headers=_headers(tokens["cashier1"]))
    assert r.status_code == 403


async def test_supervisor_can_list_transactions(api_client, tokens):
    bag = _bag("LISTME")
    await api_client.post(
        "/api/v1/transactions/", json=_payload(bag, total="170.00"), headers=_headers(tokens["cashier1"])
    )
    r = await api_client.get("/api/v1/transactions/", headers=_headers(tokens["supervisor1"]))
    assert r.status_code == 200
    assert any(t["bag_number"] == bag for t in r.json())


async def test_transactions_are_isolated_per_catalog(api_client, tokens):
    bag = _bag("ISOLATED")
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(bag, total="170.00"), headers=_headers(tokens["cashier1"], "complete")
    )
    txn_id = create.json()["transaction_id"]

    # Same transaction id looked up in a different catalog's database must not be found.
    r = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["admin"], "esnf"))
    assert r.status_code == 404
