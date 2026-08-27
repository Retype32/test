"""
Corrections must restate a bag's value, never add to it.

Corrections are append-only: correcting a bag leaves the original row in
place with is_superseded=True and adds a replacement row. Both are real
history, but only the survivor is that bag's live figure. Any surface that
sums money must therefore see only the survivor.

Real bug this covers: nothing filtered on is_superseded anywhere, so
correcting a EUR1000 bag down to EUR400 pushed the processor's recorded
cash volume UP by EUR1400 (1000 + 400), credited two slips for one bag,
and put two rows for the same bag in the exported CSV.

Uses the 'vms' catalog, whose other tests assert only on status codes, so
adding a transaction here cannot disturb the exact-sum assertions the
transaction/stats/EOD suites make in their own catalogs.
"""
import csv
import datetime
import io
import uuid
from decimal import Decimal

import pytest

CATALOG = "vms"
DENOM_LABELS = ("€500", "€200", "€100", "€50", "€20", "€10", "€5", "Coins")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": CATALOG}


def _volume(rows) -> Decimal:
    return sum((Decimal(str(r.get("cash_volume") or 0)) for r in rows), Decimal("0"))


def _slips(rows) -> int:
    return sum(r.get("slip_count") or 0 for r in rows)


@pytest.fixture
async def corrected_bag(api_client, web_client, tokens):
    """Books a EUR1000 bag then corrects it to EUR400.

    Yields the stats totals captured immediately before the bag existed, so
    each assertion can measure this bag's own contribution rather than an
    absolute figure that seeded data also feeds into.
    """
    bag = f"9400{uuid.uuid4().int % 10**8:08d}"
    h = _headers(tokens["admin"])
    today = datetime.date.today().isoformat()

    before = (await api_client.get(
        f"/api/v1/stats/processors?business_date={today}", headers=h)).json()
    baseline = (_volume(before), _slips(before))

    cust = (await api_client.get("/api/v1/customers/", headers=h)).json()[0]
    cid, lid = cust["customer_id"], cust["locations"][0]["location_id"]
    r = await api_client.post("/api/v1/transactions/", headers=h, json={
        "customer_id": cid, "location_id": lid, "bag_number": bag,
        "wallet_id": "WF", "total_value": "1000", "expected_total": "1000",
        "denominations": [{"denomination": "€500", "count": 2, "value": "1000"}]})
    assert r.status_code == 201, r.text
    txn_id = r.json()["transaction_id"]

    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": CATALOG})
    form = {"reason": "recount", "expected_total": "1000",
            "customer_id": cid, "location_id": lid,
            "bag_number": bag, "wallet_id": "WF"}
    for label in DENOM_LABELS:
        form[f"count_{label}"] = "0"
    form["count_€100"] = "4"          # corrected value: EUR400
    resp = await web_client.post(f"/web/transactions/{txn_id}/correct",
                                 data=form, follow_redirects=True)
    assert resp.status_code == 200, resp.text
    yield h, txn_id, today, baseline, bag


async def test_correction_restates_cash_volume_instead_of_adding_to_it(api_client, corrected_bag):
    h, _txn_id, today, (base_vol, base_slips), _bag = corrected_bag
    rows = (await api_client.get(
        f"/api/v1/stats/processors?business_date={today}", headers=h)).json()

    assert _volume(rows) - base_vol == Decimal("400"), (
        "a EUR1000 bag corrected to EUR400 must add EUR400 to the processor's "
        "volume; adding EUR1400 means the superseded original is still counted")
    assert _slips(rows) - base_slips == 1, (
        "one physical bag must count as one slip no matter how often it is corrected")


async def test_corrected_bag_appears_once_in_the_transaction_list(api_client, corrected_bag):
    h, _txn_id, _today, _base, bag = corrected_bag
    listed = (await api_client.get("/api/v1/transactions/", headers=h)).json()
    mine = [t for t in listed if t["bag_number"] == bag]
    assert len(mine) == 1, f"expected one live row for the bag, got {len(mine)}"
    assert Decimal(str(mine[0]["total_value"])) == Decimal("400"), \
        "the live row must carry the corrected value, not the original"


async def test_corrected_bag_appears_once_in_an_exported_report(web_client, corrected_bag):
    _h, _txn_id, today, _base, bag = corrected_bag
    rep = await web_client.get(
        f"/web/reports/download?format=csv&date_from={today}&date_to={today}")
    assert rep.status_code == 200
    rows = [r for r in csv.reader(io.StringIO(rep.text)) if bag in ",".join(r)]
    assert len(rows) == 1, (
        f"the exported report has {len(rows)} rows for one corrected bag — "
        "the pre-correction row is being double-counted")


async def test_the_superseded_original_is_still_reachable_for_audit(web_client, corrected_bag):
    """Excluding superseded rows from totals must not erase them from history."""
    _h, txn_id, _today, _base, _bag = corrected_bag
    detail = await web_client.get(f"/web/transactions/{txn_id}", follow_redirects=True)
    assert detail.status_code == 200, "the corrected-away original must stay viewable"


async def test_include_superseded_still_returns_the_full_history(corrected_bag):
    """The audit path is opt-in, not gone."""
    _h, _txn_id, _today, _base, bag = corrected_bag
    from backend.core.catalogs import CatalogCode
    from backend.core.database import get_catalog_sessionmaker
    from backend.services.transaction_service import TransactionService

    SL = get_catalog_sessionmaker(CatalogCode(CATALOG))
    async with SL() as db:
        svc = TransactionService(db)
        live = await svc.list_transactions(limit=1000)
        every = await svc.list_transactions(limit=1000, include_superseded=True)

    assert len([t for t in live if t.bag_number == bag]) == 1, \
        "default listing must hide the superseded original"
    assert len([t for t in every if t.bag_number == bag]) == 2, \
        "include_superseded must still surface the full history"
