"""Per-processor analytics: slips/hour, cash volume, error count.

Uses the 'esnf' catalog exclusively and reserved just for this file, so the
running totals asserted below (slip_count, cash_volume, error_count) stay
exact and aren't perturbed by transactions created in other test modules.

All transactions in a test run are created within milliseconds of each
other, so StatsService's active-hours span is always floored to 1.0 hour per
business-date bucket touched -- that's what makes slips_per_hour exact and
assertable here rather than a range.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

CATALOG = "esnf"
TODAY = date.today()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": CATALOG}


def _payload(bag_number: str, total: str) -> dict:
    return {
        "customer_id": "C001",
        "location_id": "L001",
        "bag_number": bag_number,
        "total_value": total,
        "denominations": [{"denomination": "€100", "count": int(Decimal(total) // 100), "value": total}],
    }


async def _create(api_client, tokens, bag, total, user="cashier1"):
    r = await api_client.post("/api/v1/transactions/", json=_payload(bag, total), headers=_headers(tokens[user]))
    assert r.status_code == 201
    return r.json()


def _row_for(rows, username):
    return next(r for r in rows if r["username"] == username)


async def test_processor_stats_end_to_end(api_client, tokens):
    dup_bag = f"STATSDUP-{uuid.uuid4().hex[:8]}"

    await _create(api_client, tokens, dup_bag, "100.00")
    await _create(api_client, tokens, f"STATS-B2-{uuid.uuid4().hex[:8]}", "200.00")
    await _create(api_client, tokens, f"STATS-B3-{uuid.uuid4().hex[:8]}", "300.00")

    summary_url = "/api/v1/stats/processors/summary"
    params = {"date_from": (TODAY - timedelta(days=30)).isoformat(), "date_to": TODAY.isoformat()}

    r = await api_client.get(summary_url, params=params, headers=_headers(tokens["supervisor1"]))
    assert r.status_code == 200
    row = _row_for(r.json(), "cashier1")
    assert row["slip_count"] == 3
    assert Decimal(row["cash_volume"]) == Decimal("600.00")
    assert row["slips_per_hour"] == 3.0
    assert row["target_per_hour"] == 23
    assert row["error_count"] == 0

    # A same-bag repeat transaction gets flagged as a pending duplicate but
    # doesn't count as an error until a supervisor confirms it.
    dup_txn = await _create(api_client, tokens, dup_bag, "100.00")

    r2 = await api_client.get(summary_url, params=params, headers=_headers(tokens["supervisor1"]))
    row2 = _row_for(r2.json(), "cashier1")
    assert row2["slip_count"] == 4
    assert Decimal(row2["cash_volume"]) == Decimal("700.00")
    assert row2["duplicate_error_count"] == 0
    assert row2["error_count"] == 0

    flags = await api_client.get("/api/v1/duplicates/", params={"status": "pending"}, headers=_headers(tokens["supervisor1"]))
    flag = next(f for f in flags.json() if f["transaction_id"] == dup_txn["transaction_id"])
    confirm = await api_client.post(
        f"/api/v1/duplicates/{flag['id']}/review", json={"status": "confirmed"}, headers=_headers(tokens["supervisor1"])
    )
    assert confirm.status_code == 200

    r3 = await api_client.get(summary_url, params=params, headers=_headers(tokens["supervisor1"]))
    row3 = _row_for(r3.json(), "cashier1")
    assert row3["slip_count"] == 4  # confirmed duplicates still count as real slips
    assert row3["duplicate_error_count"] == 1
    assert row3["transfer_error_count"] == 0
    assert row3["error_count"] == 1

    transferable = await _create(api_client, tokens, f"STATS-XFER-{uuid.uuid4().hex[:8]}", "500.00")
    r4 = await api_client.get(summary_url, params=params, headers=_headers(tokens["supervisor1"]))
    row4 = _row_for(r4.json(), "cashier1")
    assert row4["slip_count"] == 5
    assert Decimal(row4["cash_volume"]) == Decimal("1200.00")

    transfer_target = TODAY - timedelta(days=5)
    transfer = await api_client.post(
        f"/api/v1/transactions/{transferable['transaction_id']}/transfer",
        json={"new_business_date": transfer_target.isoformat(), "reason": "test transfer"},
        headers=_headers(tokens["admin"]),
    )
    assert transfer.status_code == 200

    r5 = await api_client.get(summary_url, params=params, headers=_headers(tokens["supervisor1"]))
    row5 = _row_for(r5.json(), "cashier1")
    # Same 5 slips and same total cash volume -- moving a transaction's
    # business_date doesn't remove it, it just changes which day it's
    # attributed to (and that day is still inside our query range).
    assert row5["slip_count"] == 5
    assert Decimal(row5["cash_volume"]) == Decimal("1200.00")
    assert row5["transfer_error_count"] == 1
    assert row5["error_count"] == 2  # 1 confirmed duplicate + 1 transfer
    # Two distinct business-date buckets touched (today + transfer target),
    # each floored to a minimum 1-hour active span: 5 slips / 2 hours.
    assert row5["slips_per_hour"] == 2.5


async def test_single_day_endpoint_matches_summary_for_that_day(api_client, tokens):
    await _create(api_client, tokens, f"SINGLEDAY-{uuid.uuid4().hex[:8]}", "150.00", user="cashier2")

    r = await api_client.get(
        "/api/v1/stats/processors", params={"business_date": TODAY.isoformat()}, headers=_headers(tokens["supervisor1"])
    )
    assert r.status_code == 200
    row = _row_for(r.json(), "cashier2")
    assert row["slip_count"] == 1
    assert Decimal(row["cash_volume"]) == Decimal("150.00")


async def test_cashier_cannot_view_stats(api_client, tokens):
    r = await api_client.get(
        "/api/v1/stats/processors", params={"business_date": TODAY.isoformat()}, headers=_headers(tokens["cashier1"])
    )
    assert r.status_code == 403
