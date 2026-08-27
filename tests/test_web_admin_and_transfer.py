"""Web portal admin tools: staff management (incl. self-lockout guard),
EOD close/reopen, and the admin-only transaction transfer form. Operates in
the 'vms' catalog like test_web_portal.py -- EOD closures and a business-date
transfer here don't disturb that file's read-only assertions (transfers only
move a transaction between days that are both still inside the wide date
range that file queries, and it never checks EOD-closed state).
"""
import re
from datetime import date, timedelta


async def test_admin_staff_management_and_self_lockout_guard(web_client):
    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": "vms"})

    staff_page = await web_client.get("/web/admin/users")
    assert staff_page.status_code == 200
    assert "Staff Management" in staff_page.text

    created = await web_client.post(
        "/web/admin/users",
        data={"username": "webtestuser", "password": "webtest123", "role": "cashier"},
        follow_redirects=True,
    )
    assert "created" in created.text
    assert "webtestuser" in created.text

    # The success banner mentions the username too, before the actual table
    # row -- search from the STAFF card onward to land on the row itself,
    # then pull the id off its Edit button (stable regardless of the other
    # buttons' order/markup around it).
    table_start = created.text.index("STAFF (")
    row_idx = created.text.index("webtestuser", table_start)
    row_snippet = created.text[row_idx : row_idx + 400]
    id_match = re.search(r"toggleEdit\('([0-9a-f-]{36})'\)", row_snippet)
    assert id_match is not None
    user_id = id_match.group(1)

    deactivated = await web_client.post(
        f"/web/admin/users/{user_id}/active", data={"is_active": "false"}, follow_redirects=True
    )
    assert "Disabled" in deactivated.text

    # Admin's own row shows "(you)" instead of a deactivate button.
    assert "(you)" in deactivated.text

    edited = await web_client.post(
        f"/web/admin/users/{user_id}/edit",
        data={"username": "webtestuser2", "password": "", "role": "supervisor"},
        follow_redirects=True,
    )
    assert "updated" in edited.text
    assert "webtestuser2" in edited.text
    assert "supervisor" in edited.text

    removed = await web_client.post(f"/web/admin/users/{user_id}/delete", follow_redirects=True)
    assert "removed" in removed.text.lower()
    assert "webtestuser2" not in removed.text


async def test_admin_cannot_delete_own_account_via_web(web_client, api_client, tokens):
    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": "vms"})

    users = await api_client.get(
        "/api/v1/auth/users", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )
    admin_id = next(u["id"] for u in users.json() if u["username"] == "admin")

    resp = await web_client.post(f"/web/admin/users/{admin_id}/delete", follow_redirects=True)
    assert "own account" in resp.text


async def test_admin_eod_close_and_reopen_via_web(web_client):
    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": "vms"})

    target = (date.today() - timedelta(days=3)).isoformat()

    close = await web_client.post("/web/eod/close", data={"business_date": target}, follow_redirects=True)
    assert target in close.text
    assert "CLOSED" in close.text

    reopen = await web_client.post(
        "/web/eod/reopen", data={"business_date": target, "reason": "web portal test"}, follow_redirects=True
    )
    assert "REOPENED" in reopen.text


async def test_admin_customer_and_location_management_via_web(web_client):
    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": "vms"})

    customers_page = await web_client.get("/web/admin/customers")
    assert customers_page.status_code == 200
    assert "Customer Management" in customers_page.text

    created = await web_client.post(
        "/web/admin/customers",
        data={"customer_id": "WEBTEST1", "customer_name": "Web Test Customer"},
        follow_redirects=True,
    )
    assert "created" in created.text
    assert "WEBTEST1" in created.text

    renamed = await web_client.post(
        "/web/admin/customers/WEBTEST1/edit",
        data={"new_customer_id": "WEBTEST2", "customer_name": "Web Test Customer Renamed"},
        follow_redirects=True,
    )
    assert "updated" in renamed.text
    assert "WEBTEST2" in renamed.text
    assert "Web Test Customer Renamed" in renamed.text

    detail = await web_client.get("/web/admin/customers/WEBTEST2")
    assert detail.status_code == 200
    assert "Web Test Customer Renamed" in detail.text

    loc_created = await web_client.post(
        "/web/admin/customers/WEBTEST2/locations",
        data={"location_id": "WEBTEST2-L1", "location_name": "Web Test Site"},
        follow_redirects=True,
    )
    assert "created" in loc_created.text
    assert "WEBTEST2-L1" in loc_created.text

    loc_renamed = await web_client.post(
        "/web/admin/customers/WEBTEST2/locations/WEBTEST2-L1/edit",
        data={"new_location_id": "WEBTEST2-L2", "location_name": "Web Test Site Renamed"},
        follow_redirects=True,
    )
    assert "updated" in loc_renamed.text
    assert "WEBTEST2-L2" in loc_renamed.text

    loc_removed = await web_client.post(
        "/web/admin/customers/WEBTEST2/locations/WEBTEST2-L2/delete", follow_redirects=True
    )
    assert "removed" in loc_removed.text.lower()
    locations_table = loc_removed.text[loc_removed.text.index("LOCATIONS ("):]
    assert "WEBTEST2-L2" not in locations_table

    removed = await web_client.post("/web/admin/customers/WEBTEST2/delete", follow_redirects=True)
    assert "removed" in removed.text.lower()
    customers_table = removed.text[removed.text.index("CUSTOMERS ("):]
    assert "WEBTEST2" not in customers_table


async def test_admin_transfers_transaction_via_web_form(web_client):
    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": "vms"})

    listing = await web_client.get("/web/transactions")
    match = re.search(r"/web/transactions/([0-9a-f-]{36})", listing.text)
    assert match is not None
    txn_id = match.group(1)

    detail = await web_client.get(f"/web/transactions/{txn_id}")
    assert detail.status_code == 200
    assert "TRANSFER TO ANOTHER DAY" in detail.text

    new_date = (date.today() - timedelta(days=400)).isoformat()
    transfer = await web_client.post(
        f"/web/transactions/{txn_id}/transfer",
        data={"new_business_date": new_date, "reason": "web portal transfer test"},
    )
    assert new_date in transfer.text
    assert "moved to" in transfer.text
