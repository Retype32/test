"""Cashier "New Transaction" flow in the web portal (entry -> confirm ->
create). Uses the 'dayshift' catalog with the seeded C001/L001 customer, like
test_eod_and_transfer_api.py -- any test here that closes *today* always
reopens it again in a finally block, so this stays order-independent from
that file and from later runs within this one.
"""
import re
import uuid
from datetime import date


async def test_cashier_can_create_transaction_via_web_portal(web_client, api_client, tokens):
    login = await web_client.post(
        "/web/login", data={"username": "cashier1", "password": "cash1"}, follow_redirects=False
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/web/catalog"

    select = await web_client.post("/web/catalog/select", data={"code": "dayshift"}, follow_redirects=False)
    assert select.status_code == 303
    assert select.headers["location"] == "/web/transactions/new"

    entry = await web_client.get("/web/transactions/new")
    assert entry.status_code == 200
    assert "C001" in entry.text
    assert "Tesco Ireland Ltd" in entry.text

    bag_number = f"BAG-WEBTEST-{uuid.uuid4().hex[:8]}"
    form = {
        "customer_id": "C001", "location_id": "L001", "bag_number": bag_number,
        "expected_total": "150.00",
        "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "3",
        "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
    }

    confirm = await web_client.post("/web/transactions/new/confirm", data=form)
    assert confirm.status_code == 200
    assert "BALANCED" in confirm.text
    assert "150.00" in confirm.text
    assert bag_number in confirm.text

    created = await web_client.post("/web/transactions/new", data=form)
    assert created.status_code == 200
    assert "TRANSACTION COMPLETED" in created.text
    match = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", created.text, re.S)
    assert match is not None
    txn_id = match.group(1)

    check = await api_client.get(
        f"/api/v1/transactions/{txn_id}",
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
    )
    assert check.status_code == 200
    assert check.json()["bag_number"] == bag_number
    assert check.json()["balance_status"] == "BALANCED"


async def test_transaction_creation_blocked_when_day_closed(web_client, api_client, tokens):
    today = date.today().isoformat()
    close = await api_client.post(
        "/api/v1/eod/close",
        json={"business_date": today},
        headers={"Authorization": f"Bearer {tokens['supervisor1']}", "X-Catalog": "dayshift"},
    )
    assert close.status_code == 201

    try:
        await web_client.post("/web/login", data={"username": "cashier2", "password": "cash2"})
        await web_client.post("/web/catalog/select", data={"code": "dayshift"})

        form = {
            "customer_id": "C001", "location_id": "L001", "bag_number": f"BAG-CLOSEDDAY-{uuid.uuid4().hex[:8]}",
            "expected_total": "",
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "1",
            "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
        }
        resp = await web_client.post("/web/transactions/new", data=form)
        assert resp.status_code == 400
        assert "closed" in resp.text.lower()
    finally:
        reopen = await api_client.post(
            "/api/v1/eod/reopen",
            json={"business_date": today, "reason": "test cleanup"},
            headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
        )
        assert reopen.status_code == 200
