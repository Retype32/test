"""New features: transaction correction workflow, bulk/cashier transfer,
unbalanced-first sort in the transaction viewer, automatic nightly EOD close,
and the admin database backup button.

Correction and unbalanced-sort tests use the 'complete' catalog (already
shared safely with test_transactions_api.py and
test_duplicates_and_notifications_api.py via random bag-number prefixes).
Bulk/cashier transfer uses 'dayshift' with far-past dates distinct from the
ones test_eod_and_transfer_api.py uses, so as not to collide with the day it
leaves permanently closed (today - 310 days). The automatic EOD close test
touches every catalog's "yesterday" and always reopens it again afterward.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

CATALOG = "complete"


def _headers(token: str, catalog: str = CATALOG) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": catalog}


def _bag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _payload(bag_number: str, total="170.00", expected_total="170.00"):
    return {
        "customer_id": "C001",
        "location_id": "L001",
        "bag_number": bag_number,
        "total_value": total,
        "expected_total": expected_total,
        "denominations": [
            {"denomination": "€100", "count": 1, "value": "100.00"},
            {"denomination": "€50", "count": 1, "value": "50.00"},
            {"denomination": "€20", "count": 1, "value": "20.00"},
        ],
    }


async def _web_login(web_client, username="admin", password="admin", catalog=CATALOG):
    await web_client.post("/web/login", data={"username": username, "password": password})
    await web_client.post("/web/catalog/select", data={"code": catalog})


def _correction_form(reason: str, **counts) -> dict:
    data = {
        "customer_id": "C001",
        "location_id": "L001",
        "bag_number": "CORRECTED-BAG",
        "wallet_id": "",
        "expected_total": "",
        "reason": reason,
    }
    for label in ["€500", "€200", "€100", "€50", "€20", "€10", "€5", "Coins"]:
        data[f"count_{label}"] = str(counts.get(label, 0))
    return data


async def test_correct_transaction_supersedes_original(api_client, web_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("CORRECT")), headers=_headers(tokens["cashier1"])
    )
    assert create.status_code == 201
    txn_id = create.json()["transaction_id"]

    await _web_login(web_client)

    detail = await web_client.get(f"/web/transactions/{txn_id}")
    assert "CORRECT THIS TRANSACTION" in detail.text

    form = _correction_form("miscounted the €100 note", **{"€100": 2, "€50": 1, "€20": 1})
    form["bag_number"] = "CORRECTED-BAG-1"
    corrected = await web_client.post(f"/web/transactions/{txn_id}/correct", data=form)
    assert corrected.status_code == 200
    assert "Correction saved." in corrected.text
    assert "270.00" in corrected.text  # 2*100 + 50 + 20

    original_page = await web_client.get(f"/web/transactions/{txn_id}")
    assert "has been" in original_page.text and "corrected" in original_page.text
    assert "the correction" in original_page.text

    # A superseded transaction can't be corrected again.
    again = await web_client.get(f"/web/transactions/{txn_id}/correct")
    assert "already been corrected" in again.text


async def test_correct_transaction_requires_supervisor(api_client, web_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("CORRPERM")), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    await _web_login(web_client, username="cashier1", password="cash1")
    r = await web_client.get(f"/web/transactions/{txn_id}/correct")
    assert r.status_code == 403


async def test_correct_transaction_unknown_customer_rejected(api_client, web_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("CORRBADCUST")), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    await _web_login(web_client)
    form = _correction_form("typo", **{"€50": 1})
    form["customer_id"] = "NOPE"
    r = await web_client.post(f"/web/transactions/{txn_id}/correct", data=form)
    assert "not found" in r.text
    # The form is shown again rather than a dead end, since this was a fixable input error.
    assert "SAVE CORRECTION" in r.text


async def test_unbalanced_transactions_sort_first(api_client, tokens):
    balanced_bag = _bag("SORTBAL")
    unbalanced_bag = _bag("SORTUNBAL")

    await api_client.post(
        "/api/v1/transactions/", json=_payload(balanced_bag, total="170.00", expected_total="170.00"),
        headers=_headers(tokens["cashier1"]),
    )
    await api_client.post(
        "/api/v1/transactions/", json=_payload(unbalanced_bag, total="170.00", expected_total="999.00"),
        headers=_headers(tokens["cashier1"]),
    )

    r = await api_client.get("/api/v1/transactions/", headers=_headers(tokens["supervisor1"]))
    assert r.status_code == 200
    bags = [t["bag_number"] for t in r.json()]
    assert bags.index(unbalanced_bag) < bags.index(balanced_bag)


async def test_bulk_transfer_and_cashier_transfer_via_web(api_client, web_client, tokens):
    dayshift_headers = _headers(tokens["cashier1"], catalog="dayshift")
    bag_a = _bag("BULKA")
    bag_b = _bag("BULKB")
    create_a = await api_client.post("/api/v1/transactions/", json=_payload(bag_a), headers=dayshift_headers)
    create_b = await api_client.post("/api/v1/transactions/", json=_payload(bag_b), headers=dayshift_headers)
    assert create_a.status_code == 201 and create_b.status_code == 201
    txn_a_id = create_a.json()["transaction_id"]
    txn_b_id = create_b.json()["transaction_id"]

    new_date = (date.today() - timedelta(days=600)).isoformat()
    await _web_login(web_client, catalog="dayshift")

    bulk = await web_client.post(
        "/web/transactions/bulk-transfer",
        data={
            "transaction_ids": [txn_a_id, txn_b_id],
            "new_business_date": new_date,
            "reason": "bulk test transfer",
        },
    )
    assert bulk.status_code == 200
    assert "2 transaction(s) moved" in bulk.text

    detail_a = await web_client.get(f"/web/transactions/{txn_a_id}")
    assert new_date in detail_a.text

    # Now exercise "transfer all of one cashier's transactions" on a fresh pair.
    bag_c = _bag("CASHXFER")
    create_c = await api_client.post("/api/v1/transactions/", json=_payload(bag_c), headers=dayshift_headers)
    assert create_c.status_code == 201
    txn_c_id = create_c.json()["transaction_id"]

    users = await api_client.get("/api/v1/auth/users", headers=_headers(tokens["admin"], catalog="dayshift"))
    cashier1_id = next(u["id"] for u in users.json() if u["username"] == "cashier1")

    new_date_2 = (date.today() - timedelta(days=601)).isoformat()
    cashier_transfer = await web_client.post(
        "/web/transactions/transfer-cashier",
        data={
            "cashier_user_id": cashier1_id,
            "business_date": date.today().isoformat(),
            "new_business_date": new_date_2,
            "reason": "move this cashier's whole day",
        },
    )
    assert cashier_transfer.status_code == 200
    assert "transaction(s) moved from" in cashier_transfer.text

    detail_c = await web_client.get(f"/web/transactions/{txn_c_id}")
    assert new_date_2 in detail_c.text


async def test_auto_close_all_catalogs_marks_automatic_and_reopen_still_works(api_client, tokens):
    from backend.services.eod_scheduler import auto_close_all_catalogs

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        await auto_close_all_catalogs()

        status = await api_client.get(
            "/api/v1/eod/status", params={"business_date": yesterday}, headers=_headers(tokens["admin"], "esnf")
        )
        assert status.json()["closed"] is True

        closures = await api_client.get(
            "/api/v1/eod/", params={"date_from": yesterday, "date_to": yesterday},
            headers=_headers(tokens["admin"], "esnf"),
        )
        assert closures.status_code == 200
        closure = next(c for c in closures.json() if c["business_date"] == yesterday)
        assert closure["closed_automatically"] is True
        assert closure["closed_by_user_id"] is None

        # An admin can still reopen an automatically-closed day.
        reopened = await api_client.post(
            "/api/v1/eod/reopen",
            json={"business_date": yesterday, "reason": "test cleanup"},
            headers=_headers(tokens["admin"], "esnf"),
        )
        assert reopened.status_code == 200
        assert reopened.json()["status"] == "reopened"
    finally:
        for catalog in ("vms", "dayshift", "complete", "esnf"):
            await api_client.post(
                "/api/v1/eod/reopen",
                json={"business_date": yesterday, "reason": "test cleanup"},
                headers=_headers(tokens["admin"], catalog),
            )


async def test_correction_does_not_double_count_in_stats(api_client, web_client, tokens):
    """A corrected transaction replaces the original: the superseded row must
    drop out of slip counts and cash volume, or the books show the wrong
    figure plus its own correction.

    Scoped to a throwaway user so the assertions stay exact regardless of what
    other modules have written into this catalog.
    """
    username = f"statscorr-{uuid.uuid4().hex[:8]}"
    created_user = await api_client.post(
        "/api/v1/auth/users",
        json={"username": username, "password": "corrtest123", "role": "cashier"},
        headers=_headers(tokens["admin"]),
    )
    assert created_user.status_code == 201

    login = await api_client.post(
        "/api/v1/auth/login", json={"username": username, "password": "corrtest123"}
    )
    user_token = login.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}", "X-Catalog": CATALOG}

    a = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("DBLA"), total="100.00", expected_total="100.00"),
        headers=user_headers,
    )
    await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("DBLB"), total="200.00", expected_total="200.00"),
        headers=user_headers,
    )
    txn_a_id = a.json()["transaction_id"]

    params = {
        "date_from": (date.today() - timedelta(days=1)).isoformat(),
        "date_to": date.today().isoformat(),
    }
    before = await api_client.get(
        "/api/v1/stats/processors/summary", params=params, headers=_headers(tokens["supervisor1"])
    )
    row_before = next(r for r in before.json() if r["username"] == username)
    assert row_before["slip_count"] == 2
    assert Decimal(row_before["cash_volume"]) == Decimal("300.00")

    # Correct A from €100 up to €500.
    await _web_login(web_client)
    form = _correction_form("recount: was short by 400", **{"€100": 5})
    corrected = await web_client.post(f"/web/transactions/{txn_a_id}/correct", data=form)
    assert "Correction saved." in corrected.text

    after = await api_client.get(
        "/api/v1/stats/processors/summary", params=params, headers=_headers(tokens["supervisor1"])
    )
    row_after = next(r for r in after.json() if r["username"] == username)
    # Still 2 slips (the correction replaces the original, it doesn't add to
    # it), and the volume reflects the corrected figure: 500 + 200.
    assert row_after["slip_count"] == 2
    assert Decimal(row_after["cash_volume"]) == Decimal("700.00")


async def test_superseded_transaction_excluded_from_report_export(api_client, web_client, tokens):
    bag = _bag("RPTDUP")
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(bag), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    await _web_login(web_client)
    form = _correction_form("fix count for report test", **{"€100": 3})
    form["bag_number"] = bag  # correction keeps the same bag number
    assert "Correction saved." in (await web_client.post(f"/web/transactions/{txn_id}/correct", data=form)).text

    report = await web_client.get("/web/reports/download", params={"format": "csv"})
    assert report.status_code == 200
    body = report.content.decode("utf-8")
    # Exactly one row for this bag -- the correction. The superseded original
    # must not also appear, or the bag is counted twice in the export.
    assert body.count(bag) == 1


async def test_bulk_transfer_with_nothing_selected_shows_message(web_client):
    await _web_login(web_client, catalog="dayshift")
    r = await web_client.post(
        "/web/transactions/bulk-transfer",
        data={"new_business_date": (date.today() - timedelta(days=700)).isoformat(), "reason": "none selected"},
    )
    assert r.status_code == 200
    assert "Select at least one transaction" in r.text


async def test_admin_backup_creates_files(web_client):
    import os
    from backend.services import backup_service

    await _web_login(web_client)
    before = {b["name"] for b in backup_service.list_backups()}

    resp = await web_client.post("/web/admin/backup")
    assert resp.status_code == 200
    assert "Backup created" in resp.text

    after = backup_service.list_backups()
    new_names = {b["name"] for b in after} - before
    assert len(new_names) == 1
    new_backup = next(b for b in after if b["name"] in new_names)
    assert new_backup["file_count"] == 5

    # Clean up the folder this test created so repeated runs don't pile up.
    import shutil
    shutil.rmtree(os.path.join(backup_service.BACKUP_ROOT, new_backup["name"]))
