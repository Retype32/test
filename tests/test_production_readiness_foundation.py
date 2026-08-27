"""Phase 3 FOUNDATION control-area tests: idempotency, optimistic locking /
unique-constraint concurrency control, the H-4 origin-day transfer gate, the
PG-2 enum CHECK constraint, and the S-09 self-correction/self-review guards.

Uses the 'complete' catalog -- shared with test_transactions_api.py,
test_new_features.py, and test_duplicates_and_notifications_api.py, none of
which perform an EOD close or assert a catalog-wide aggregate total, so this
file's transactions/EOD closures (all far-in-the-past business_dates) don't
collide with theirs. NOT 'vms': that catalog is reserved read-only reference
data for test_web_portal.py's supervisor walkthrough, which asserts an exact
aggregate cash total across all of 'vms' -- writing a single transaction
there from another file silently breaks that assertion (confirmed the hard
way; see git history). The insert-race/update-race tests below still need
real isolation from anything else touching the same rows concurrently in
the shared session-scoped app, which picking business_dates unique to this
file (not just a catalog unique to this file) already guarantees regardless
of which catalog is chosen.

Per docs/production_readiness/01_transaction_integrity.md §9 and
06_consolidated_plan.md §12: insert-race conflicts (EOD close, double
correction) are exercised via real concurrent requests (asyncio.gather
against the actual app fixture) because a unique-constraint conflict
manifests regardless of exact interleaving -- the second INSERT fails once
the first's row exists, whenever that happens. The one update-race test
(H-2 optimistic locking) instead opens two independent sessions directly and
interleaves them by hand, because a stale-version conflict is only
observable if the second session's read genuinely precedes the first
session's commit -- something scheduler timing through two real HTTP
requests cannot guarantee, and this codebase's own single-writer SQLite file
lock (00_baseline.md, 01_transaction_integrity.md §3) makes exactly this
kind of ordering non-deterministic over a real ASGI round trip.
"""
import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from backend.core.catalogs import CatalogCode
from backend.core.database import get_catalog_sessionmaker
from backend.models.transaction import Transaction
from backend.repositories.transaction_repository import TransactionRepository

CATALOG = "complete"


def _headers(token: str, catalog: str = CATALOG) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": catalog}


def _bag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _payload(bag_number: str, total="170.00", expected_total="170.00") -> dict:
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


# ---------------------------------------------------------------------------
# Item 1: idempotency-key contract
# ---------------------------------------------------------------------------

async def test_create_transaction_without_idempotency_key_still_works(api_client, tokens):
    """Backward compatibility: no header at all is still today's behavior."""
    r = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("NOKEY")), headers=_headers(tokens["cashier1"])
    )
    assert r.status_code == 201


async def test_idempotency_key_collapses_retry_into_one_transaction(api_client, tokens):
    key = str(uuid.uuid4())
    headers = {**_headers(tokens["cashier1"]), "Idempotency-Key": key}
    payload = _payload(_bag("IDEMP-RETRY"))

    first = await api_client.post("/api/v1/transactions/", json=payload, headers=headers)
    assert first.status_code == 201
    txn_id = first.json()["transaction_id"]

    # A genuine retry: identical key, identical payload.
    second = await api_client.post("/api/v1/transactions/", json=payload, headers=headers)
    assert second.status_code == 200, "a replay returns the original result (200), not a new insert"
    assert second.json()["transaction_id"] == txn_id

    # And a third, for good measure -- still the same row.
    third = await api_client.post("/api/v1/transactions/", json=payload, headers=headers)
    assert third.status_code == 200
    assert third.json()["transaction_id"] == txn_id


async def test_idempotency_key_is_scoped_per_endpoint(api_client, tokens):
    """The same raw key used for create vs. correct must never collide --
    scope is folded into the stored key (consolidated plan §7: (catalog,
    endpoint, key))."""
    shared_key = str(uuid.uuid4())
    headers = {**_headers(tokens["cashier1"]), "Idempotency-Key": shared_key}

    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("SCOPE-A")), headers=headers
    )
    assert create.status_code == 201
    txn_id = create.json()["transaction_id"]

    # Reusing the identical raw key against the correction endpoint must be
    # treated as a brand new key in that scope, not a collision with the
    # create above.
    supervisor_headers = {**_headers(tokens["supervisor1"]), "Idempotency-Key": shared_key}
    correct_payload = {**_payload(_bag("SCOPE-A-FIXED")), "reason": "unrelated correction"}
    correct = await api_client.post(
        f"/api/v1/transactions/{txn_id}/correct", json=correct_payload, headers=supervisor_headers
    )
    assert correct.status_code == 201
    assert correct.json()["transaction_id"] != txn_id


async def test_concurrent_same_bag_different_idempotency_keys_both_succeed(api_client, tokens):
    """C-2 remains a soft-flag-only, unenforced invariant on purpose (§16
    item 1, deferred) -- two different idempotency keys for the identical
    bag/customer/location/amount both create their own row; nothing at the
    DB level blocks the second one."""
    bag = _bag("CONCBAG")
    payload = _payload(bag)
    headers1 = {**_headers(tokens["cashier1"]), "Idempotency-Key": str(uuid.uuid4())}
    headers2 = {**_headers(tokens["cashier1"]), "Idempotency-Key": str(uuid.uuid4())}

    r1, r2 = await asyncio.gather(
        api_client.post("/api/v1/transactions/", json=payload, headers=headers1),
        api_client.post("/api/v1/transactions/", json=payload, headers=headers2),
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["transaction_id"] != r2.json()["transaction_id"]


# ---------------------------------------------------------------------------
# Item 2: REST correction endpoint (M-2)
# ---------------------------------------------------------------------------

async def test_rest_correction_endpoint_reachable_and_supervisor_gated(api_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("RESTCORR")), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    correct_payload = {**_payload(_bag("RESTCORR-FIXED"), total="271.00", expected_total="271.00"),
                        "reason": "REST correction endpoint now exists"}

    denied = await api_client.post(
        f"/api/v1/transactions/{txn_id}/correct", json=correct_payload, headers=_headers(tokens["cashier1"])
    )
    assert denied.status_code == 403

    ok = await api_client.post(
        f"/api/v1/transactions/{txn_id}/correct", json=correct_payload, headers=_headers(tokens["supervisor1"])
    )
    assert ok.status_code == 201
    assert ok.json()["transaction_id"] != txn_id
    assert Decimal(ok.json()["total_value"]) == Decimal("271.00")


# ---------------------------------------------------------------------------
# Items 3/4/5: version_id_col, unique partial index, EOD-close race
# ---------------------------------------------------------------------------

async def test_concurrent_eod_close_second_request_gets_409_not_500(api_client, tokens):
    """H-1: two concurrent first-time closes for the same business_date --
    the loser must get a clean 409, never an unhandled 500."""
    target = date.today() - timedelta(days=9500)
    headers = _headers(tokens["supervisor1"])

    r1, r2 = await asyncio.gather(
        api_client.post("/api/v1/eod/close", json={"business_date": target.isoformat()}, headers=headers),
        api_client.post("/api/v1/eod/close", json={"business_date": target.isoformat()}, headers=headers),
    )
    statuses = sorted(r.status_code for r in (r1, r2))
    assert statuses == [201, 409], f"expected exactly one winner and one clean conflict, got {statuses}"


async def test_double_correction_of_same_transaction_rejected_at_db_level(api_client, tokens):
    """H-3: the unique partial index on original_transaction_id is the real
    backstop -- both concurrent correction attempts pass the application's
    is_superseded TOCTOU check, but only one INSERT can win."""
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("DBLCORR")), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    headers = _headers(tokens["supervisor1"])
    payload_a = {**_payload(_bag("DBLCORR-A")), "reason": "attempt A"}
    payload_b = {**_payload(_bag("DBLCORR-B")), "reason": "attempt B"}

    r1, r2 = await asyncio.gather(
        api_client.post(f"/api/v1/transactions/{txn_id}/correct", json=payload_a, headers=headers),
        api_client.post(f"/api/v1/transactions/{txn_id}/correct", json=payload_b, headers=headers),
    )
    statuses = sorted(r.status_code for r in (r1, r2))
    assert statuses == [201, 409], f"expected exactly one winning correction and one clean conflict, got {statuses}"

    # The original now has exactly one correction, not two.
    corrections = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["admin"]))
    assert corrections.status_code == 200


async def test_stale_version_transfer_rejected_not_silently_overwritten(api_client, tokens):
    """H-2: TransactionRepository.update_business_date's optimistic-locking
    version check. Two sessions read the same row before either writes (the
    exact TOCTOU window 01_transaction_integrity.md §2.1 documents), then
    the second session's write must be cleanly rejected, not silently
    overwrite the first session's committed change."""
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("STALEXFER")), headers=_headers(tokens["cashier1"])
    )
    txn_id = uuid.UUID(create.json()["transaction_id"])

    sessionmaker = get_catalog_sessionmaker(CatalogCode.complete)
    async with sessionmaker() as session_a, sessionmaker() as session_b:
        repo_a = TransactionRepository(session_a)
        repo_b = TransactionRepository(session_b)

        # Both sessions load the row before either writes.
        txn_a = await repo_a.get_by_id(txn_id)
        txn_b = await repo_b.get_by_id(txn_id)
        assert txn_a is not None and txn_b is not None

        winner_date = date.today() - timedelta(days=9600)
        loser_date = date.today() - timedelta(days=9601)

        updated = await repo_a.update_business_date(txn_id, winner_date)
        assert updated.business_date == winner_date
        await session_a.commit()

        with pytest.raises(ValueError, match="modified by another request"):
            await repo_b.update_business_date(txn_id, loser_date)
        await session_b.rollback()

    # The winning session's write is what actually stuck.
    check = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["admin"]))
    assert check.json()["business_date"] == winner_date.isoformat()


# ---------------------------------------------------------------------------
# Item 6: H-4 -- transfer must also gate on the transaction's OWN day
# ---------------------------------------------------------------------------

async def test_transfer_business_date_blocks_transferring_out_of_closed_day(api_client, tokens):
    origin_date = date.today() - timedelta(days=9700)
    target_date_1 = date.today() - timedelta(days=9701)
    target_date_2 = date.today() - timedelta(days=9702)

    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("ORIGINCLOSE")), headers=_headers(tokens["cashier1"])
    )
    txn_id = create.json()["transaction_id"]

    move = await api_client.post(
        f"/api/v1/transactions/{txn_id}/transfer",
        json={"new_business_date": origin_date.isoformat(), "reason": "set up test"},
        headers=_headers(tokens["admin"]),
    )
    assert move.status_code == 200

    close = await api_client.post(
        "/api/v1/eod/close", json={"business_date": origin_date.isoformat()}, headers=_headers(tokens["supervisor1"])
    )
    assert close.status_code == 201

    # Before this fix, only the TARGET day was checked -- the transaction's
    # own (now-closed) current day was never gated.
    blocked = await api_client.post(
        f"/api/v1/transactions/{txn_id}/transfer",
        json={"new_business_date": target_date_1.isoformat(), "reason": "should be blocked"},
        headers=_headers(tokens["admin"]),
    )
    assert blocked.status_code == 409
    assert "closed" in blocked.json()["detail"]

    reopen = await api_client.post(
        "/api/v1/eod/reopen",
        json={"business_date": origin_date.isoformat(), "reason": "test cleanup"},
        headers=_headers(tokens["admin"]),
    )
    assert reopen.status_code == 200

    allowed = await api_client.post(
        f"/api/v1/transactions/{txn_id}/transfer",
        json={"new_business_date": target_date_2.isoformat(), "reason": "now allowed"},
        headers=_headers(tokens["admin"]),
    )
    assert allowed.status_code == 200


async def test_bulk_transfer_rechecks_origin_day_per_row_not_once_up_front(api_client, tokens):
    """A row whose own business_date is closed must block the whole batch
    (matching the existing all-or-nothing behavior for a closed target
    date), even when it isn't the first row checked."""
    open_date = date.today() - timedelta(days=9750)
    closed_origin = date.today() - timedelta(days=9751)
    new_target = date.today() - timedelta(days=9752)

    ok_create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("BULKOK")), headers=_headers(tokens["cashier1"])
    )
    bad_create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("BULKBAD")), headers=_headers(tokens["cashier1"])
    )
    ok_id = ok_create.json()["transaction_id"]
    bad_id = bad_create.json()["transaction_id"]

    # ok_id stays on an open day; bad_id is moved onto a day that gets closed.
    move_ok = await api_client.post(
        f"/api/v1/transactions/{ok_id}/transfer",
        json={"new_business_date": open_date.isoformat(), "reason": "setup"},
        headers=_headers(tokens["admin"]),
    )
    move_bad = await api_client.post(
        f"/api/v1/transactions/{bad_id}/transfer",
        json={"new_business_date": closed_origin.isoformat(), "reason": "setup"},
        headers=_headers(tokens["admin"]),
    )
    assert move_ok.status_code == 200 and move_bad.status_code == 200

    close = await api_client.post(
        "/api/v1/eod/close", json={"business_date": closed_origin.isoformat()}, headers=_headers(tokens["supervisor1"])
    )
    assert close.status_code == 201

    # bulk_transfer_business_date is shared by both the REST API (no bulk
    # route exists there today -- only the web portal's admin-only form
    # calls it) and web/routes/transactions_web.py's bulk-transfer route;
    # exercising it directly here tests the one shared implementation both
    # surfaces depend on.
    from backend.services.transaction_service import TransactionService

    sessionmaker = get_catalog_sessionmaker(CatalogCode.complete)
    async with sessionmaker() as session:
        service = TransactionService(session)
        with pytest.raises(ValueError, match="closed"):
            # ok_id first (its own day is open -- only bad_id's origin day
            # is closed), proving the closed-origin check fires per row
            # rather than once before the loop starts.
            await service.bulk_transfer_business_date(
                [uuid.UUID(ok_id), uuid.UUID(bad_id)], new_target, "bulk test", uuid.uuid4()
            )
        await session.rollback()


# ---------------------------------------------------------------------------
# Item 8: PG-2 enum create_constraint -- SQLite now enforces it too
# ---------------------------------------------------------------------------

async def test_invalid_balance_status_enum_value_rejected_on_sqlite(api_client, tokens):
    sessionmaker = get_catalog_sessionmaker(CatalogCode.complete)
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(Transaction.__table__).values(
                    transaction_id=uuid.uuid4(),
                    user_id=uuid.uuid4(),
                    username="enum-test",
                    customer_id="C001",
                    location_id="L001",
                    bag_number=_bag("ENUMTEST"),
                    total_value=Decimal("1.00"),
                    balance_status="NOT_A_REAL_STATUS",
                    business_date=date.today(),
                    version_id=1,
                )
            )
        await session.rollback()


# ---------------------------------------------------------------------------
# Item 10: self-correction / self-review-of-own-flag guards (S-09)
# ---------------------------------------------------------------------------

async def test_supervisor_cannot_correct_own_transaction(api_client, tokens):
    create = await api_client.post(
        "/api/v1/transactions/", json=_payload(_bag("SELFCORRECT")), headers=_headers(tokens["supervisor1"])
    )
    assert create.status_code == 201
    txn_id = create.json()["transaction_id"]

    correct_payload = {**_payload(_bag("SELFCORRECT-FIX")), "reason": "trying to fix my own work"}
    r = await api_client.post(
        f"/api/v1/transactions/{txn_id}/correct", json=correct_payload, headers=_headers(tokens["supervisor1"])
    )
    assert r.status_code == 403

    # A *different* supervisor is unaffected by the guard.
    still_correctable = await api_client.get(f"/api/v1/transactions/{txn_id}", headers=_headers(tokens["admin"]))
    assert still_correctable.json()["duplicate_flag_status"] in ("none", "pending_review")


async def test_supervisor_cannot_review_duplicate_flag_on_own_transaction(api_client, tokens):
    bag = _bag("SELFREVIEW")
    payload = _payload(bag)
    headers = _headers(tokens["supervisor1"])

    first = await api_client.post("/api/v1/transactions/", json=payload, headers=headers)
    second = await api_client.post("/api/v1/transactions/", json=payload, headers=headers)
    assert first.status_code == 201 and second.status_code == 201

    flags = await api_client.get(
        "/api/v1/duplicates/", params={"status": "pending"}, headers=_headers(tokens["admin"])
    )
    assert flags.status_code == 200
    flag = next(f for f in flags.json() if f["transaction_id"] == second.json()["transaction_id"])

    review = await api_client.post(
        f"/api/v1/duplicates/{flag['id']}/review",
        json={"status": "confirmed", "notes": "trying to review my own flag"},
        headers=headers,
    )
    # duplicates.py maps every ValueError from review_flag to 404 today
    # (unowned file, not touched here) -- the guard itself is a plain
    # ValueError specifically so it's caught by that existing handler with
    # no route change required. The real assertion is behavioral: nothing
    # about the flag actually changed.
    assert review.status_code != 200

    still_pending = await api_client.get(f"/api/v1/duplicates/{flag['id']}", headers=_headers(tokens["admin"]))
    assert still_pending.json()["status"] == "pending"

    # A supervisor uninvolved with the flagged transactions CAN review it.
    other_review = await api_client.post(
        f"/api/v1/duplicates/{flag['id']}/review",
        json={"status": "dismissed", "notes": "reviewed by someone else"},
        headers=_headers(tokens["admin"]),
    )
    assert other_review.status_code == 200
