"""EOD close/reopen locking and admin day-transfer.

Uses the 'dayshift' catalog exclusively. Any test that closes *today* always
reopens it again in a finally block, so tests remain order-independent and
don't leak a closed "today" into other test modules or later runs within
this one.
"""
import uuid
from datetime import date, timedelta

CATALOG = "dayshift"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": CATALOG}


def _payload(bag_number: str) -> dict:
    return {
        "customer_id": "C001",
        "location_id": "L001",
        "bag_number": bag_number,
        "total_value": "170.00",
        "denominations": [
            {"denomination": "€100", "count": 1, "value": "100.00"},
            {"denomination": "€50", "count": 1, "value": "50.00"},
            {"denomination": "€20", "count": 1, "value": "20.00"},
        ],
    }


async def test_day_status_open_by_default(api_client, tokens):
    far_future = date.today() + timedelta(days=500)
    r = await api_client.get(
        "/api/v1/eod/status", params={"business_date": far_future.isoformat()}, headers=_headers(tokens["admin"])
    )
    assert r.status_code == 200
    assert r.json()["closed"] is False


async def test_cashier_cannot_close_day(api_client, tokens):
    target = (date.today() - timedelta(days=200)).isoformat()
    r = await api_client.post("/api/v1/eod/close", json={"business_date": target}, headers=_headers(tokens["cashier1"]))
    assert r.status_code == 403


async def test_supervisor_can_close_day_admin_can_reopen(api_client, tokens):
    target = date.today() - timedelta(days=201)

    r = await api_client.post(
        "/api/v1/eod/close", json={"business_date": target.isoformat()}, headers=_headers(tokens["supervisor1"])
    )
    assert r.status_code == 201
    assert r.json()["status"] == "closed"

    status = await api_client.get(
        "/api/v1/eod/status", params={"business_date": target.isoformat()}, headers=_headers(tokens["admin"])
    )
    assert status.json()["closed"] is True

    # Closing an already-closed day is rejected.
    dup = await api_client.post(
        "/api/v1/eod/close", json={"business_date": target.isoformat()}, headers=_headers(tokens["supervisor1"])
    )
    assert dup.status_code == 409

    # Supervisors cannot reopen -- admin only.
    forbidden = await api_client.post(
        "/api/v1/eod/reopen",
        json={"business_date": target.isoformat(), "reason": "not allowed"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert forbidden.status_code == 403

    reopened = await api_client.post(
        "/api/v1/eod/reopen",
        json={"business_date": target.isoformat(), "reason": "test cleanup"},
        headers=_headers(tokens["admin"]),
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "reopened"

    # Reopening a day that isn't currently closed is rejected.
    again = await api_client.post(
        "/api/v1/eod/reopen",
        json={"business_date": target.isoformat(), "reason": "already open"},
        headers=_headers(tokens["admin"]),
    )
    assert again.status_code == 409


async def test_closed_today_blocks_transaction_creation(api_client, tokens):
    today = date.today().isoformat()
    r = await api_client.post("/api/v1/eod/close", json={"business_date": today}, headers=_headers(tokens["supervisor1"]))
    assert r.status_code == 201
    try:
        create = await api_client.post(
            "/api/v1/transactions/", json=_payload(f"CLOSEDDAY-{uuid.uuid4().hex[:8]}"), headers=_headers(tokens["cashier1"])
        )
        assert create.status_code == 400
        assert "closed" in create.json()["detail"]
    finally:
        reopen = await api_client.post(
            "/api/v1/eod/reopen",
            json={"business_date": today, "reason": "test cleanup"},
            headers=_headers(tokens["admin"]),
        )
        assert reopen.status_code == 200


async def test_list_closures_requires_supervisor(api_client, tokens):
    r = await api_client.get("/api/v1/eod/", headers=_headers(tokens["cashier1"]))
    assert r.status_code == 403

    r2 = await api_client.get("/api/v1/eod/", headers=_headers(tokens["supervisor1"]))
    assert r2.status_code == 200


async def test_admin_transfers_transaction_to_open_day(api_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(f"XFER-{uuid.uuid4().hex[:8]}"), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]
    new_date = date.today() - timedelta(days=300)

    r = await api_client.post(
        f"/api/v1/transactions/{txn_id}/transfer",
        json={"new_business_date": new_date.isoformat(), "reason": "fixing a mistake"},
        headers=_headers(tokens["admin"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["business_date"] == new_date.isoformat()


async def test_non_admin_cannot_transfer_transaction(api_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(f"XFERPERM-{uuid.uuid4().hex[:8]}"), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    r = await api_client.post(
        f"/api/v1/transactions/{txn_id}/transfer",
        json={"new_business_date": date.today().isoformat(), "reason": "nope"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert r.status_code == 403


async def test_cannot_transfer_into_a_closed_day(api_client, tokens):
    closed_target = date.today() - timedelta(days=310)
    close = await api_client.post(
        "/api/v1/eod/close", json={"business_date": closed_target.isoformat()}, headers=_headers(tokens["supervisor1"])
    )
    assert close.status_code == 201

    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(f"XFERBLOCKED-{uuid.uuid4().hex[:8]}"), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    r = await api_client.post(
        f"/api/v1/transactions/{txn_id}/transfer",
        json={"new_business_date": closed_target.isoformat(), "reason": "should fail"},
        headers=_headers(tokens["admin"]),
    )
    assert r.status_code == 409
    assert "closed" in r.json()["detail"]


async def test_transfer_nonexistent_transaction_404(api_client, tokens):
    r = await api_client.post(
        f"/api/v1/transactions/{uuid.uuid4()}/transfer",
        json={"new_business_date": date.today().isoformat(), "reason": "n/a"},
        headers=_headers(tokens["admin"]),
    )
    assert r.status_code == 404
