"""Server-rendered web portal: auth gate, catalog picker, and the read-only
supervisor pages (dashboard, transactions, reports, EOD, notifications,
duplicates, stats). Read-only against the 'vms' catalog, which is the only
catalog seeded with demo transactions -- these assertions (bag numbers, cash
volume) depend on that seed data staying untouched, so no other test file
should write transactions into 'vms'.
"""
import re


async def test_unauthenticated_dashboard_redirects_to_login(web_client):
    r = await web_client.get("/web/dashboard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/web/login"


async def test_unknown_web_page_renders_404_html(web_client):
    r = await web_client.get("/web/nonexistent-page")
    assert r.status_code == 404
    assert "NOT FOUND" in r.text


async def test_login_form_loads(web_client):
    r = await web_client.get("/web/login")
    assert r.status_code == 200
    assert "SIGN IN" in r.text


async def test_cashier_login_rejected_by_web_portal(web_client):
    r = await web_client.post("/web/login", data={"username": "cashier1", "password": "cash1"})
    assert r.status_code == 403
    assert "supervisors and administrators only" in r.text


async def test_supervisor_walkthrough_of_read_only_pages(web_client):
    login = await web_client.post(
        "/web/login", data={"username": "supervisor1", "password": "super"}, follow_redirects=False
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/web/catalog"

    catalog_page = await web_client.get("/web/catalog")
    assert catalog_page.status_code == 200
    assert "VMS" in catalog_page.text

    select = await web_client.post("/web/catalog/select", data={"code": "vms"}, follow_redirects=False)
    assert select.status_code == 303
    assert select.headers["location"] == "/web/dashboard"

    dashboard = await web_client.get("/web/dashboard")
    assert dashboard.status_code == 200
    assert "Business Day" in dashboard.text

    transactions = await web_client.get("/web/transactions")
    assert transactions.status_code == 200
    assert "BAG-2026-001" in transactions.text

    reports = await web_client.get("/web/reports")
    assert reports.status_code == 200
    assert "EXPORT TO EXCEL" in reports.text

    csv_download = await web_client.get("/web/reports/download", params={"format": "csv"})
    assert csv_download.status_code == 200
    assert "csv" in csv_download.headers.get("content-type", "")
    assert b"BAG-2026-001" in csv_download.content

    eod_page = await web_client.get("/web/eod")
    assert eod_page.status_code == 200
    assert "CLOSE A BUSINESS DAY" in eod_page.text

    # Supervisors are blocked from the admin-only staff page.
    admin_page = await web_client.get("/web/admin/users")
    assert admin_page.status_code == 403
    assert "ACCESS DENIED" in admin_page.text

    notifications_page = await web_client.get("/web/notifications")
    assert notifications_page.status_code == 200

    duplicates_page = await web_client.get("/web/duplicates")
    assert duplicates_page.status_code == 200

    stats_page = await web_client.get("/web/stats", params={"date_from": "2020-01-01", "date_to": "2035-12-31"})
    assert stats_page.status_code == 200
    assert "cashier1" in stats_page.text
    assert "4090.00" in stats_page.text


async def test_transaction_detail_page_loads(web_client):
    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": "vms"})

    listing = await web_client.get("/web/transactions")
    match = re.search(r"/web/transactions/([0-9a-f-]{36})", listing.text)
    assert match is not None
    txn_id = match.group(1)

    detail = await web_client.get(f"/web/transactions/{txn_id}")
    assert detail.status_code == 200
    assert "TRANSFER TO ANOTHER DAY" in detail.text
