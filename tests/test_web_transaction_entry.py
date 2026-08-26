"""Cashier "New Transaction" flow in the web portal. The flow is a four-step
wizard (Customer & Location -> Bag & Amount -> Wallet ID -> Cash Count ->
Confirm -> Complete), each step its own page, with the in-progress
transaction kept server-side in the session between them. Uses the
'dayshift' catalog with the seeded C001/L001 customer, like
test_eod_and_transfer_api.py -- any test here that closes *today* always
reopens it again in a finally block, so this stays order-independent from
that file and from later runs within this one.
"""
import re
import uuid
from datetime import date
from decimal import Decimal


def _random_bag_number() -> str:
    # 13 digits, "310"-prefixed -- matches the wizard's bag-number format.
    return "310" + f"{uuid.uuid4().int % 10**10:010d}"


async def _login_and_select_catalog(web_client, username, password):
    login = await web_client.post(
        "/web/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert login.status_code == 303
    select = await web_client.post("/web/catalog/select", data={"code": "dayshift"}, follow_redirects=False)
    assert select.status_code == 303
    assert select.headers["location"] == "/web/transactions/new"


async def test_machine_count_endpoint_returns_denominations_on_success(web_client, monkeypatch):
    """POST /transactions/new/count is the wizard's "Count with machine"
    hop -- it wraps hardware.wait_for_shared_count() in a thread and hands
    the result straight to the browser. Nothing else in the suite exercises
    this route at all, so a break anywhere in that chain (route wiring,
    the asyncio.to_thread call, the JSON shape) would go unnoticed."""
    import hardware

    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    monkeypatch.setattr(
        hardware, "wait_for_shared_count",
        lambda timeout_seconds=120: hardware.CountResult(denominations={"€50": 4, "Coins": 2}),
    )

    resp = await web_client.post("/web/transactions/new/count")
    assert resp.status_code == 200
    assert resp.json() == {"denominations": {"€50": 4, "Coins": 2}}


async def test_machine_count_endpoint_surfaces_the_specific_connection_error(web_client, monkeypatch):
    """The wizard's status badge shows this JSON's "error" string verbatim
    (wizard_cash.html's pollLoop). A generic "not connected" here would
    silently undo the fix that makes GDC1ReportCounter's specific reason
    (wrong port / already in use) reach the operator's screen."""
    import hardware

    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    def _raise(timeout_seconds=120):
        raise ConnectionError("Serial port COM3 does not exist on this PC.")

    monkeypatch.setattr(hardware, "wait_for_shared_count", _raise)

    resp = await web_client.post("/web/transactions/new/count")
    assert resp.status_code == 200
    assert resp.json() == {"error": "Serial port COM3 does not exist on this PC."}


async def test_machine_ping_endpoint_confirms_a_live_connection(web_client, monkeypatch):
    """POST /transactions/new/ping is the non-blocking liveness check the
    wizard's cash step calls when it starts listening and again after every
    batch. It must succeed without ever touching wait_for_count_result."""
    import hardware

    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    monkeypatch.setattr(hardware, "ping_shared_counter", lambda: None)

    resp = await web_client.post("/web/transactions/new/ping")
    assert resp.status_code == 200
    assert resp.json() == {"connected": True}


async def test_machine_ping_endpoint_surfaces_the_specific_connection_error(web_client, monkeypatch):
    import hardware

    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    def _raise():
        raise ConnectionError("Serial port COM3 does not exist on this PC.")

    monkeypatch.setattr(hardware, "ping_shared_counter", _raise)

    resp = await web_client.post("/web/transactions/new/ping")
    assert resp.status_code == 200
    assert resp.json() == {
        "connected": False,
        "error": "Serial port COM3 does not exist on this PC.",
    }


async def test_cashier_can_create_transaction_via_wizard(web_client, api_client, tokens):
    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    launch = await web_client.get("/web/transactions/new")
    assert launch.status_code == 200
    assert "Start Transaction" in launch.text

    step1 = await web_client.get("/web/transactions/new/wizard/customer")
    assert step1.status_code == 200

    lookup = await web_client.get("/web/transactions/new/wizard/customer-lookup", params={"customer_id": "C001"})
    assert lookup.status_code == 200
    lookup_data = lookup.json()
    assert lookup_data["found"] is True
    assert lookup_data["customer_name"] == "Tesco Ireland Ltd"
    assert any(loc["location_id"] == "L001" for loc in lookup_data["locations"])

    customer_post = await web_client.post(
        "/web/transactions/new/wizard/customer",
        data={"customer_id": "C001", "location_id": "L001"},
        follow_redirects=False,
    )
    assert customer_post.status_code == 303
    assert customer_post.headers["location"] == "/web/transactions/new/wizard/bag"

    step2 = await web_client.get("/web/transactions/new/wizard/bag")
    assert step2.status_code == 200

    bag_number = _random_bag_number()
    bag_post = await web_client.post(
        "/web/transactions/new/wizard/bag",
        data={"bag_number": bag_number, "amount": "150.00"},
        follow_redirects=False,
    )
    assert bag_post.status_code == 303
    assert bag_post.headers["location"] == "/web/transactions/new/wizard/wallet"

    step3 = await web_client.get("/web/transactions/new/wizard/wallet")
    assert step3.status_code == 200

    wallet_id = f"WALLET-{uuid.uuid4().hex[:8]}"
    wallet_post = await web_client.post(
        "/web/transactions/new/wizard/wallet",
        data={"wallet_id": wallet_id},
        follow_redirects=False,
    )
    assert wallet_post.status_code == 303
    assert wallet_post.headers["location"] == "/web/transactions/new/wizard/cash"

    step4 = await web_client.get("/web/transactions/new/wizard/cash")
    assert step4.status_code == 200

    cash_form = {
        "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "3",
        "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
    }
    confirm = await web_client.post("/web/transactions/new/wizard/cash", data=cash_form)
    assert confirm.status_code == 200
    assert "BALANCED" in confirm.text
    assert bag_number in confirm.text
    assert wallet_id in confirm.text

    completed = await web_client.post("/web/transactions/new/wizard/complete")
    assert completed.status_code == 200
    assert "BALANCED" in completed.text
    assert wallet_id in completed.text
    match = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", completed.text, re.S)
    assert match is not None
    txn_id = match.group(1)

    check = await api_client.get(
        f"/api/v1/transactions/{txn_id}",
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
    )
    assert check.status_code == 200
    data = check.json()
    assert data["bag_number"] == bag_number
    assert data["wallet_id"] == wallet_id
    assert data["balance_status"] == "BALANCED"


async def test_shortage_transaction_via_wizard_persists_as_not_balanced(web_client, api_client, tokens):
    # Regression test: balance_status must reflect actual counted cash vs the
    # declared bag amount, not just agree with itself by construction.
    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    await web_client.get("/web/transactions/new/wizard/customer")
    await web_client.post(
        "/web/transactions/new/wizard/customer",
        data={"customer_id": "C001", "location_id": "L001"},
    )

    bag_number = _random_bag_number()
    await web_client.post(
        "/web/transactions/new/wizard/bag",
        data={"bag_number": bag_number, "amount": "150.00"},
    )
    await web_client.post(
        "/web/transactions/new/wizard/wallet",
        data={"wallet_id": f"WALLET-{uuid.uuid4().hex[:8]}"},
    )

    # Declared 150.00 but only counts 100.00 -- a genuine shortage.
    cash_form = {
        "count_500": "0", "count_200": "0", "count_100": "1", "count_50": "0",
        "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
    }
    confirm = await web_client.post("/web/transactions/new/wizard/cash", data=cash_form)
    assert confirm.status_code == 200
    assert "SHORTAGE" in confirm.text

    completed = await web_client.post("/web/transactions/new/wizard/complete")
    assert completed.status_code == 200
    assert "SHORTAGE" in completed.text
    match = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", completed.text, re.S)
    assert match is not None
    txn_id = match.group(1)

    check = await api_client.get(
        f"/api/v1/transactions/{txn_id}",
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
    )
    assert check.status_code == 200
    data = check.json()
    assert data["balance_status"] == "NOT BALANCED"
    assert Decimal(data["total_value"]) == Decimal("100.00")
    assert Decimal(data["expected_total"]) == Decimal("150.00")


async def test_complete_transaction_starts_next_wallet_on_same_bag(web_client, api_client, tokens):
    # "Complete Transaction" on the result screen opens a fresh, fully
    # independent transaction for the same bag -- same customer/location/
    # bag_number, but its own wallet_id and expected_total, saved as its own
    # row with nothing linking it back to the first (so a mistake on one
    # wallet never has to be untangled from the others).
    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    await web_client.get("/web/transactions/new/wizard/customer")
    await web_client.post(
        "/web/transactions/new/wizard/customer",
        data={"customer_id": "C001", "location_id": "L001"},
    )

    bag_number = _random_bag_number()
    await web_client.post(
        "/web/transactions/new/wizard/bag",
        data={"bag_number": bag_number, "amount": "50.00"},
    )
    wallet_1 = f"WALLET-{uuid.uuid4().hex[:8]}"
    await web_client.post(
        "/web/transactions/new/wizard/wallet",
        data={"wallet_id": wallet_1},
    )
    await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "1",
            "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
        },
    )
    first = await web_client.post("/web/transactions/new/wizard/complete")
    assert first.status_code == 200
    match = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", first.text, re.S)
    assert match is not None
    txn_id_1 = match.group(1)

    next_form = await web_client.get("/web/transactions/new/wizard/wallet/next")
    assert next_form.status_code == 200
    assert bag_number in next_form.text

    wallet_2 = f"WALLET-{uuid.uuid4().hex[:8]}"
    next_post = await web_client.post(
        "/web/transactions/new/wizard/wallet/next",
        data={"wallet_id": wallet_2, "amount": "30.00"},
        follow_redirects=False,
    )
    assert next_post.status_code == 303
    assert next_post.headers["location"] == "/web/transactions/new/wizard/cash"

    await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "0",
            "count_20": "1", "count_10": "1", "count_5": "0", "count_coins": "0",
        },
    )
    second = await web_client.post("/web/transactions/new/wizard/complete")
    assert second.status_code == 200
    match2 = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", second.text, re.S)
    assert match2 is not None
    txn_id_2 = match2.group(1)
    assert txn_id_1 != txn_id_2

    check1 = await api_client.get(
        f"/api/v1/transactions/{txn_id_1}",
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
    )
    check2 = await api_client.get(
        f"/api/v1/transactions/{txn_id_2}",
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
    )
    data1, data2 = check1.json(), check2.json()

    assert data1["bag_number"] == data2["bag_number"] == bag_number
    assert data1["customer_id"] == data2["customer_id"] == "C001"
    assert data1["location_id"] == data2["location_id"] == "L001"
    assert data1["wallet_id"] == wallet_1
    assert data2["wallet_id"] == wallet_2
    assert Decimal(data1["expected_total"]) == Decimal("50.00")
    assert Decimal(data2["expected_total"]) == Decimal("30.00")
    assert Decimal(data1["total_value"]) == Decimal("50.00")
    assert Decimal(data2["total_value"]) == Decimal("30.00")


async def test_cash_step_rejects_non_numeric_denomination_input(web_client, api_client, tokens):
    # A typo in a count field must surface a clear error rather than
    # silently treating it as zero and understating the total.
    await _login_and_select_catalog(web_client, "cashier1", "cash1")

    await web_client.get("/web/transactions/new/wizard/customer")
    await web_client.post(
        "/web/transactions/new/wizard/customer",
        data={"customer_id": "C001", "location_id": "L001"},
    )
    await web_client.post(
        "/web/transactions/new/wizard/bag",
        data={"bag_number": _random_bag_number(), "amount": "50.00"},
    )
    await web_client.post(
        "/web/transactions/new/wizard/wallet",
        data={"wallet_id": f"WALLET-{uuid.uuid4().hex[:8]}"},
    )

    resp = await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "1o",
            "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
        },
    )
    assert resp.status_code == 400
    assert "whole number" in resp.text.lower()


async def test_transaction_creation_blocked_when_day_closed(web_client, api_client, tokens):
    today = date.today().isoformat()
    close = await api_client.post(
        "/api/v1/eod/close",
        json={"business_date": today},
        headers={"Authorization": f"Bearer {tokens['supervisor1']}", "X-Catalog": "dayshift"},
    )
    assert close.status_code == 201

    try:
        await _login_and_select_catalog(web_client, "cashier2", "cash2")

        await web_client.get("/web/transactions/new/wizard/customer")
        await web_client.post(
            "/web/transactions/new/wizard/customer",
            data={"customer_id": "C001", "location_id": "L001"},
        )
        await web_client.post(
            "/web/transactions/new/wizard/bag",
            data={"bag_number": _random_bag_number(), "amount": "50.00"},
        )
        await web_client.post(
            "/web/transactions/new/wizard/wallet",
            data={"wallet_id": f"WALLET-{uuid.uuid4().hex[:8]}"},
        )
        await web_client.post(
            "/web/transactions/new/wizard/cash",
            data={
                "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "1",
                "count_20": "0", "count_10": "0", "count_5": "0", "count_coins": "0",
            },
        )
        resp = await web_client.post("/web/transactions/new/wizard/complete")
        assert resp.status_code == 400
        assert "closed" in resp.text.lower()
    finally:
        reopen = await api_client.post(
            "/api/v1/eod/reopen",
            json={"business_date": today, "reason": "test cleanup"},
            headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "dayshift"},
        )
        assert reopen.status_code == 200
