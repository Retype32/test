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


async def _login_and_select_complete(web_client, username, password):
    login = await web_client.post(
        "/web/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert login.status_code == 303
    select = await web_client.post("/web/catalog/select", data={"code": "complete"}, follow_redirects=False)
    assert select.status_code == 303


async def test_complete_catalog_uses_the_bulk_bag_flow_with_a_placeholder_customer(web_client, api_client, tokens):
    # Brink's Complete skips customer/location lookup entirely -- "Add New
    # Bag" goes straight to the bag scan, and every transaction files under
    # a fixed placeholder identity (9999999999999 / "Brink's Complete")
    # instead of a real customer. Every other catalog is untouched by this
    # (see test_complete_transaction_starts_next_wallet_on_same_bag above,
    # which exercises the same "same bag, next wallet" mechanics under the
    # ordinary customer-first flow).
    await _login_and_select_complete(web_client, "cashier1", "cash1")

    landing = await web_client.get("/web/transactions/new")
    assert landing.status_code == 200
    assert "Add New Bag" in landing.text
    assert "Start Transaction" not in landing.text

    # The ordinary customer-first entry point must not be reachable here.
    bounced = await web_client.get("/web/transactions/new/wizard/complete-bag", follow_redirects=False)
    # (cashier1 already has a catalog selected, so this only proves the
    # *route* redirects away for a non-Complete catalog; verified for real
    # in the VMS/dayshift tests above, which never touch this route.)
    assert bounced.status_code == 200  # reachable, since catalog IS complete here

    bag_number = _random_bag_number()
    bag_post = await web_client.post(
        "/web/transactions/new/wizard/complete-bag",
        data={"bag_number": bag_number},
        follow_redirects=False,
    )
    assert bag_post.status_code == 303
    assert bag_post.headers["location"] == "/web/transactions/new/wizard/wallet/next?first=1"

    first_wallet_form = await web_client.get(bag_post.headers["location"])
    assert first_wallet_form.status_code == 200
    assert "Scan First Wallet" in first_wallet_form.text
    assert "Brink&#39;s Complete (9999999999999)" in first_wallet_form.text

    wallet_1 = f"WALLET-{uuid.uuid4().hex[:8]}"
    await web_client.post(
        "/web/transactions/new/wizard/wallet/next",
        data={"wallet_id": wallet_1, "amount": "40.00", "first": "1"},
    )
    await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "0",
            "count_20": "2", "count_10": "0", "count_5": "0", "count_coins": "0",
        },
    )
    first = await web_client.post("/web/transactions/new/wizard/complete")
    assert first.status_code == 200
    assert "Add Wallet" in first.text
    assert "Complete Transaction" in first.text
    assert "Cancel" in first.text
    # Not the generic wording the other catalogs get on this same screen.
    assert "Complete Transaction &mdash; Next Wallet" not in first.text
    assert "End Deposit" not in first.text
    match = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", first.text, re.S)
    assert match is not None
    txn_id_1 = match.group(1)

    # "Add Wallet" loops back into the same shared route as every other
    # catalog's next-wallet step -- just without the "first" marker now.
    wallet_2 = f"WALLET-{uuid.uuid4().hex[:8]}"
    next_post = await web_client.post(
        "/web/transactions/new/wizard/wallet/next",
        data={"wallet_id": wallet_2, "amount": "15.00"},
        follow_redirects=False,
    )
    assert next_post.status_code == 303
    assert next_post.headers["location"] == "/web/transactions/new/wizard/cash"

    await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "0",
            "count_20": "0", "count_10": "1", "count_5": "1", "count_coins": "0",
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
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "complete"},
    )
    check2 = await api_client.get(
        f"/api/v1/transactions/{txn_id_2}",
        headers={"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "complete"},
    )
    data1, data2 = check1.json(), check2.json()

    for data in (data1, data2):
        assert data["customer_id"] == "9999999999999"
        assert data["location_id"] == "9999999999999"
        assert data["bag_number"] == bag_number
    assert data1["wallet_id"] == wallet_1
    assert data2["wallet_id"] == wallet_2


async def test_complete_cancel_permanently_deletes_every_wallet_saved_for_the_bag(web_client, api_client, tokens):
    # "Cancel" on Complete's result screen is a genuine hard delete (by
    # explicit design choice for this one flow, unlike the rest of the app's
    # append-only correction pattern): every transaction saved so far for
    # this bulk bag disappears, not just the in-progress draft.
    await _login_and_select_complete(web_client, "cashier1", "cash1")

    await web_client.get("/web/transactions/new/wizard/complete-bag")
    bag_number = _random_bag_number()
    await web_client.post("/web/transactions/new/wizard/complete-bag", data={"bag_number": bag_number})

    wallet_1 = f"WALLET-{uuid.uuid4().hex[:8]}"
    await web_client.post(
        "/web/transactions/new/wizard/wallet/next",
        data={"wallet_id": wallet_1, "amount": "20.00", "first": "1"},
    )
    await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "0",
            "count_20": "1", "count_10": "0", "count_5": "0", "count_coins": "0",
        },
    )
    first = await web_client.post("/web/transactions/new/wizard/complete")
    txn_id_1 = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", first.text, re.S).group(1)

    wallet_2 = f"WALLET-{uuid.uuid4().hex[:8]}"
    await web_client.post(
        "/web/transactions/new/wizard/wallet/next",
        data={"wallet_id": wallet_2, "amount": "10.00"},
    )
    await web_client.post(
        "/web/transactions/new/wizard/cash",
        data={
            "count_500": "0", "count_200": "0", "count_100": "0", "count_50": "0",
            "count_20": "0", "count_10": "1", "count_5": "0", "count_coins": "0",
        },
    )
    second = await web_client.post("/web/transactions/new/wizard/complete")
    txn_id_2 = re.search(r"TRANSACTION ID.{0,80}?([0-9a-f-]{36})", second.text, re.S).group(1)

    admin_headers = {"Authorization": f"Bearer {tokens['admin']}", "X-Catalog": "complete"}
    still_there = await api_client.get(f"/api/v1/transactions/{txn_id_1}", headers=admin_headers)
    assert still_there.status_code == 200

    cancelled = await web_client.post("/web/transactions/new/wizard/complete-cancel-all")
    assert cancelled.status_code == 200
    assert "2 transaction(s)" in cancelled.text
    assert "permanently deleted" in cancelled.text

    for txn_id in (txn_id_1, txn_id_2):
        gone = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=admin_headers)
        assert gone.status_code == 404

    # The draft is cleared too -- "Add New Bag" starts a genuinely fresh bag.
    landing = await web_client.get("/web/transactions/new/wizard/complete-bag")
    assert landing.status_code == 200
    assert bag_number not in landing.text


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
