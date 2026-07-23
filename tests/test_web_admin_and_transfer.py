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

    # The success banner also mentions the username, before the actual table
    # row -- rfind finds the row itself, which has the deactivate form.
    idx = created.text.rfind("webtestuser")
    snippet = created.text[idx : idx + 600]
    form_match = re.search(r'action="(/web/admin/users/[0-9a-f-]{36}/active)"', snippet)
    assert form_match is not None
    deactivate_url = form_match.group(1)

    deactivated = await web_client.post(deactivate_url, data={"is_active": "false"}, follow_redirects=True)
    assert "Disabled" in deactivated.text

    # Admin's own row shows "(you)" instead of a deactivate button.
    assert "(you)" in deactivated.text


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
