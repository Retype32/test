"""Duplicate-transaction detection and the notification feed it raises.

Shares the 'complete' catalog with test_transactions_api.py; every payload
here uses a bag number unique to this file so it can never accidentally
match (and get flagged as a duplicate of) something created there.
"""
import uuid
from datetime import date, timedelta

CATALOG = "complete"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": CATALOG}


def _payload(bag_number: str, total="170.00") -> dict:
    return {
        "customer_id": "C001",
        "location_id": "L001",
        "bag_number": bag_number,
        "total_value": total,
        "denominations": [
            {"denomination": "€100", "count": 1, "value": "100.00"},
            {"denomination": "€50", "count": 1, "value": "50.00"},
            {"denomination": "€20", "count": 1, "value": "20.00"},
        ],
    }


async def _create(api_client, tokens, bag_number, total="170.00", user="cashier1"):
    r = await api_client.post("/api/v1/transactions/", json=_payload(bag_number, total), headers=_headers(tokens[user]))
    assert r.status_code == 201
    return r.json()


async def test_identical_second_transaction_flags_both_as_duplicates(api_client, tokens):
    bag = f"DUPFLAG-{uuid.uuid4().hex[:8]}"
    original = await _create(api_client, tokens, bag)
    second = await _create(api_client, tokens, bag)  # same bag/customer/location/total/denominations

    original_after = await api_client.get(f"/api/v1/transactions/{original['transaction_id']}", headers=_headers(tokens["admin"]))
    second_after = await api_client.get(f"/api/v1/transactions/{second['transaction_id']}", headers=_headers(tokens["admin"]))
    assert original_after.json()["duplicate_flag_status"] == "pending_review"
    assert second_after.json()["duplicate_flag_status"] == "pending_review"

    flags = await api_client.get("/api/v1/duplicates/", params={"status": "pending"}, headers=_headers(tokens["supervisor1"]))
    assert flags.status_code == 200
    matching = [f for f in flags.json() if f["transaction_id"] == second["transaction_id"]]
    assert len(matching) == 1
    assert matching[0]["duplicate_of_transaction_id"] == original["transaction_id"]
    assert set(matching[0]["matched_fields"]) == {"customer_id", "location_id", "bag_number", "total_value", "denominations"}


async def test_different_transactions_are_not_flagged(api_client, tokens):
    await _create(api_client, tokens, f"NODUP-A-{uuid.uuid4().hex[:8]}", total="170.00")
    txn = await _create(api_client, tokens, f"NODUP-B-{uuid.uuid4().hex[:8]}", total="250.00")

    check = await api_client.get(f"/api/v1/transactions/{txn['transaction_id']}", headers=_headers(tokens["admin"]))
    assert check.json()["duplicate_flag_status"] == "none"


async def test_duplicate_raises_notification_for_supervisors(api_client, tokens):
    bag = f"DUPNOTIFY-{uuid.uuid4().hex[:8]}"
    await _create(api_client, tokens, bag)
    second = await _create(api_client, tokens, bag)

    notifications = await api_client.get(
        "/api/v1/notifications/", params={"status": "open", "event_type": "DUPLICATE_TRANSACTION"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert notifications.status_code == 200
    assert any(n["related_transaction_id"] == second["transaction_id"] for n in notifications.json())

    unread = await api_client.get("/api/v1/notifications/unread-count", headers=_headers(tokens["supervisor1"]))
    assert unread.json()["count"] >= 1


async def test_cashier_cannot_view_duplicates_or_notifications(api_client, tokens):
    r1 = await api_client.get("/api/v1/duplicates/", headers=_headers(tokens["cashier1"]))
    assert r1.status_code == 403
    r2 = await api_client.get("/api/v1/notifications/", headers=_headers(tokens["cashier1"]))
    assert r2.status_code == 403


async def test_confirming_duplicate_updates_both_transactions_and_resolves_notification(api_client, tokens):
    bag = f"DUPCONFIRM-{uuid.uuid4().hex[:8]}"
    original = await _create(api_client, tokens, bag)
    second = await _create(api_client, tokens, bag)

    flags = await api_client.get("/api/v1/duplicates/", params={"status": "pending"}, headers=_headers(tokens["supervisor1"]))
    flag = next(f for f in flags.json() if f["transaction_id"] == second["transaction_id"])

    review = await api_client.post(
        f"/api/v1/duplicates/{flag['id']}/review",
        json={"status": "confirmed", "notes": "confirmed real duplicate"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert review.status_code == 200
    assert review.json()["status"] == "confirmed"

    for txn_id in (original["transaction_id"], second["transaction_id"]):
        r = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["admin"]))
        assert r.json()["duplicate_flag_status"] == "confirmed_duplicate"

    notification = await api_client.get(
        "/api/v1/notifications/", params={"status": "resolved", "event_type": "DUPLICATE_TRANSACTION"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert any(n["related_transaction_id"] == second["transaction_id"] for n in notification.json())


async def test_dismissing_duplicate_marks_both_transactions_dismissed(api_client, tokens):
    bag = f"DUPDISMISS-{uuid.uuid4().hex[:8]}"
    original = await _create(api_client, tokens, bag)
    second = await _create(api_client, tokens, bag)

    flags = await api_client.get("/api/v1/duplicates/", params={"status": "pending"}, headers=_headers(tokens["supervisor1"]))
    flag = next(f for f in flags.json() if f["transaction_id"] == second["transaction_id"])

    review = await api_client.post(
        f"/api/v1/duplicates/{flag['id']}/review",
        json={"status": "dismissed", "notes": "false alarm, different bags"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert review.status_code == 200
    assert review.json()["status"] == "dismissed"

    for txn_id in (original["transaction_id"], second["transaction_id"]):
        r = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["admin"]))
        assert r.json()["duplicate_flag_status"] == "dismissed"


async def test_get_nonexistent_duplicate_flag_404(api_client, tokens):
    r = await api_client.get(f"/api/v1/duplicates/{uuid.uuid4()}", headers=_headers(tokens["supervisor1"]))
    assert r.status_code == 404


async def test_transfer_raises_transaction_transferred_notification(api_client, tokens):
    txn = await _create(api_client, tokens, f"XFERNOTIFY-{uuid.uuid4().hex[:8]}")
    new_date = date.today() - timedelta(days=350)

    transfer = await api_client.post(
        f"/api/v1/transactions/{txn['transaction_id']}/transfer",
        json={"new_business_date": new_date.isoformat(), "reason": "wrong day entered"},
        headers=_headers(tokens["admin"]),
    )
    assert transfer.status_code == 200

    notifications = await api_client.get(
        "/api/v1/notifications/", params={"event_type": "TRANSACTION_TRANSFERRED"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert any(n["related_transaction_id"] == txn["transaction_id"] for n in notifications.json())
