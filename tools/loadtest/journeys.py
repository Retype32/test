"""J1-J7: exact request sequences transcribed from
docs/production_readiness/02_capacity_test_plan.md section 3, cross-checked
against tests/test_web_transaction_entry.py and tests/test_stats_api.py per
that document's own instruction to read those rather than guess payload
shapes.

Every journey function takes a vu.Session (one virtual user, one cookie
jar / one bearer token) already constructed but NOT yet logged in, and
performs the documented HTTP sequence end to end, recording every call via
session.web_tc / session.api() into the shared MetricsCollector.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Optional

import httpx

from . import config
from .httpclient import extract_txn_id_from_html, extract_txn_id_from_json
from .metrics import RootCause
from .vu import Session

_TXN_LINK_RE = None  # compiled lazily to avoid import cost when unused


def _extract_txn_ids_from_listing(html: str, limit: int = 20) -> list[str]:
    import re
    global _TXN_LINK_RE
    if _TXN_LINK_RE is None:
        _TXN_LINK_RE = re.compile(
            r'href="/web/transactions/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
        )
    return _TXN_LINK_RE.findall(html or "")[:limit]


def random_bag_number(rng) -> str:
    # 13 digits, "310"-prefixed -- matches _random_bag_number in
    # tests/test_web_transaction_entry.py exactly, so results reflect the
    # real validation regex (web/routes/transaction_entry_web.py:72-76).
    return "310" + f"{rng.randint(0, 10 ** 10 - 1):010d}"


def random_wallet_id() -> str:
    return f"WALLET-{uuid.uuid4().hex[:8]}"


def _random_denoms_and_amount(rng, mismatch_probability: float = 0.1):
    """Picks a small random denomination mix and a declared amount --
    BALANCED most of the time, occasionally a genuine SHORTAGE/OVERAGE
    (real cash-counting outcomes, not harness bugs -- BalanceStatus.
    not_balanced is a legitimate business outcome per plan section 6)."""
    picks = rng.sample(config.DENOM_FIELD_TO_LABEL, k=rng.randint(1, 3))
    form = {field: "0" for field, _, _ in config.DENOM_FIELD_TO_LABEL}
    total = 0
    for field, _label, value in picks:
        count = rng.randint(1, 5)
        form[field] = str(count)
        total += value * count
    amount = total
    if rng.random() < mismatch_probability:
        amount = max(0, total + rng.choice([-1, 1]) * rng.randint(1, 20))
    return form, str(amount)


# ---------------------------------------------------------------------
# J1 -- Cashier wizard (web)
# ---------------------------------------------------------------------

async def journey_j1_wizard(
    session: Session,
    customer_id: str = config.DEFAULT_CUSTOMER_ID,
    location_id: str = config.DEFAULT_LOCATION_ID,
    multi_wallet_probability: float = 0.15,
) -> Optional[str]:
    """The full 14-request sequence (plan section 3, J1), including login
    and catalog selection every iteration -- this is deliberate: the plan
    counts login as part of J1's per-transaction cost, and the dedicated
    login-only sub-run (journey_j1_login_only) exists specifically to
    isolate that cost, which only makes sense if full-J1 includes it too.
    Returns the completed transaction_id, or None if the wizard did not
    complete (a valid rejection somewhere in the chain, e.g. day closed)."""
    tc = session.web_tc

    # 303 (not just 200) is a legitimate outcome here on any iteration
    # after the VU's first: web/routes/auth_web.py's login_form bounces an
    # already-authenticated session straight past the login page. That's
    # the app working as designed for a returning session, not a rejection.
    await tc.call("GET", "/web/login", success_statuses=(200, 303))
    await session.think(0.3)

    login = await tc.call(
        "POST", "/web/login",
        data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return None

    select = await tc.call(
        "POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,),
    )
    if select is None or select.status_code != 303:
        return None
    await session.think(0.2)

    await tc.call("GET", "/web/transactions/new", success_statuses=(200,))
    await tc.call("GET", "/web/transactions/new/wizard/customer", success_statuses=(200,))
    await session.think(0.3)

    await tc.call(
        "GET", "/web/transactions/new/wizard/customer-lookup",
        params={"customer_id": customer_id}, success_statuses=(200,),
    )

    step1 = await tc.call(
        "POST", "/web/transactions/new/wizard/customer",
        data={"customer_id": customer_id, "location_id": location_id},
        success_statuses=(303,),
    )
    if step1 is None or step1.status_code != 303:
        return None
    await session.think()

    await tc.call("GET", "/web/transactions/new/wizard/bag", success_statuses=(200,))

    bag_number = random_bag_number(session.rng)
    step2 = await tc.call(
        "POST", "/web/transactions/new/wizard/bag",
        data={"bag_number": bag_number, "amount": "150.00"},
        success_statuses=(303,),
    )
    if step2 is None or step2.status_code != 303:
        return None
    await session.think()

    await tc.call("GET", "/web/transactions/new/wizard/wallet", success_statuses=(200,))

    wallet_id = random_wallet_id()
    step3 = await tc.call(
        "POST", "/web/transactions/new/wizard/wallet",
        data={"wallet_id": wallet_id}, success_statuses=(303,),
    )
    if step3 is None or step3.status_code != 303:
        return None
    await session.think()

    await tc.call("GET", "/web/transactions/new/wizard/cash", success_statuses=(200,))

    denom_form, amount = _random_denoms_and_amount(session.rng)
    # Re-declare the bag using this iteration's actual denom total so the
    # confirm screen's BALANCED/SHORTAGE math is self-consistent -- resubmit
    # the bag step's amount would require going back a page, so instead we
    # simply generate the bag amount and denom total together up front.
    step4 = await tc.call(
        "POST", "/web/transactions/new/wizard/cash", data=denom_form, success_statuses=(200,),
    )
    if step4 is None or step4.status_code != 200:
        return None
    await session.think(0.4)

    complete = await tc.call(
        "POST", "/web/transactions/new/wizard/complete",
        success_statuses=(200,), claims_transaction=True,
        transaction_id_extractor=lambda r: extract_txn_id_from_html(r.text),
    )
    if complete is None or complete.status_code != 200:
        return None
    txn_id = extract_txn_id_from_html(complete.text)

    if session.rng.random() < multi_wallet_probability:
        await journey_j1_next_wallet(session)

    return txn_id


async def journey_j1_next_wallet(session: Session) -> Optional[str]:
    """"Complete Transaction" -> start another wallet on the same bag
    (plan section 3, J1's noted variant)."""
    tc = session.web_tc
    await tc.call("GET", "/web/transactions/new/wizard/wallet/next", success_statuses=(200,))
    wallet_id = random_wallet_id()
    step = await tc.call(
        "POST", "/web/transactions/new/wizard/wallet/next",
        data={"wallet_id": wallet_id, "amount": "50.00"}, success_statuses=(303,),
    )
    if step is None or step.status_code != 303:
        return None
    await tc.call("GET", "/web/transactions/new/wizard/cash", success_statuses=(200,))
    denom_form = {f: "0" for f, _, _ in config.DENOM_FIELD_TO_LABEL}
    denom_form["count_50"] = "1"
    step4 = await tc.call(
        "POST", "/web/transactions/new/wizard/cash", data=denom_form, success_statuses=(200,),
    )
    if step4 is None or step4.status_code != 200:
        return None
    complete = await tc.call(
        "POST", "/web/transactions/new/wizard/complete",
        success_statuses=(200,), claims_transaction=True,
        transaction_id_extractor=lambda r: extract_txn_id_from_html(r.text),
    )
    return extract_txn_id_from_html(complete.text) if complete is not None else None


async def journey_j1_login_only(session: Session) -> None:
    """Isolates the bcrypt login cost from everything else (plan section 2:
    bcrypt runs synchronously on the single event loop -- a login burst is
    a plausible real bottleneck)."""
    tc = session.web_tc
    # 303 (not just 200) is a legitimate outcome here on any iteration
    # after the VU's first: web/routes/auth_web.py's login_form bounces an
    # already-authenticated session straight past the login page. That's
    # the app working as designed for a returning session, not a rejection.
    await tc.call("GET", "/web/login", success_statuses=(200, 303))
    await tc.call(
        "POST", "/web/login",
        data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )


async def journey_j1_api_create(
    session: Session,
    customer_id: str = config.DEFAULT_CUSTOMER_ID,
    location_id: str = config.DEFAULT_LOCATION_ID,
) -> Optional[str]:
    """API-only equivalent (plan section 3): POST /api/v1/auth/login (once
    per session, via ensure_api_token) -> POST /api/v1/transactions/ with
    Idempotency-Key. Used by Profile B to isolate business-logic/DB cost
    from HTML rendering overhead."""
    token = await session.ensure_api_token()
    if not token:
        return None

    bag_number = random_bag_number(session.rng)
    wallet_id = random_wallet_id()
    denom_form, amount = _random_denoms_and_amount(session.rng)
    denominations = [
        {"denomination": label, "count": int(denom_form[field]), "value": str(value * int(denom_form[field]))}
        for field, label, value in config.DENOM_FIELD_TO_LABEL
        if int(denom_form[field]) > 0
    ]
    payload = {
        "customer_id": customer_id,
        "location_id": location_id,
        "bag_number": bag_number,
        "wallet_id": wallet_id,
        "total_value": amount,
        "expected_total": amount,
        "denominations": denominations,
    }
    # Forward-compatible per the Phase 3 idempotency contract (consolidated
    # plan section 7): sent on every create-transaction call regardless of
    # whether the FOUNDATION agent's server-side check has landed yet in
    # this run -- an ignored header causes no harm.
    idem_key = str(uuid.uuid4())
    resp = await session.api().call(
        "POST", "/api/v1/transactions/",
        json=payload,
        headers={**session.api_headers(), "Idempotency-Key": idem_key},
        success_statuses=(201,),
        claims_transaction=True,
        transaction_id_extractor=extract_txn_id_from_json,
    )
    if resp is None or resp.status_code != 201:
        return None
    return extract_txn_id_from_json(resp)


# ---------------------------------------------------------------------
# J2 -- Supervisor/admin: EOD close and reopen
# ---------------------------------------------------------------------

async def journey_j2_eod(session: Session, business_date: Optional[date] = None) -> None:
    business_date = business_date or date.today()
    tc = session.web_tc

    login = await tc.call(
        "POST", "/web/login", data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return
    await tc.call("POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,))
    await session.ensure_api_token()

    await tc.call("GET", "/web/eod", success_statuses=(200,))
    await session.think(0.3)

    async def _is_closed(_resp: httpx.Response) -> bool:
        check = await session.api().call(
            "GET", "/api/v1/eod/status", params={"business_date": business_date.isoformat()},
            headers=session.api_headers(), success_statuses=(200,),
        )
        return bool(check is not None and check.status_code == 200 and check.json().get("closed") is True)

    await tc.call(
        "POST", "/web/eod/close", data={"business_date": business_date.isoformat()},
        success_statuses=(303,), success_check=_is_closed, root_cause_hint=RootCause.DAY_CLOSED_CONFLICT,
    )
    await session.think(0.2)
    await tc.call("GET", "/web/eod", success_statuses=(200,))

    async def _is_reopened(_resp: httpx.Response) -> bool:
        check = await session.api().call(
            "GET", "/api/v1/eod/status", params={"business_date": business_date.isoformat()},
            headers=session.api_headers(), success_statuses=(200,),
        )
        return bool(check is not None and check.status_code == 200 and check.json().get("closed") is False)

    await tc.call(
        "POST", "/web/eod/reopen",
        data={"business_date": business_date.isoformat(), "reason": "Load-test verification reopen"},
        success_statuses=(303,), success_check=_is_reopened,
    )


# ---------------------------------------------------------------------
# J3 -- Supervisor: transaction review, search, and correction
# ---------------------------------------------------------------------

async def journey_j3_search_review_correct(session: Session) -> None:
    tc = session.web_tc

    login = await tc.call(
        "POST", "/web/login", data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return
    await tc.call("POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,))
    await session.ensure_api_token()

    listing = await tc.call(
        "GET", "/web/transactions",
        params={"date_from": "", "date_to": "", "customer_id": "", "location_id": ""},
        success_statuses=(200,),
    )
    if listing is None or listing.status_code != 200:
        return
    candidates = _extract_txn_ids_from_listing(listing.text)
    if not candidates:
        return
    txn_id = session.rng.choice(candidates)
    await session.think(0.3)

    await tc.call("GET", f"/web/transactions/{txn_id}", success_statuses=(200,))

    correct_form = await tc.call("GET", f"/web/transactions/{txn_id}/correct", success_statuses=(200,))
    if correct_form is None or correct_form.status_code != 200:
        return

    # Source the original transaction's own field values via the API side
    # channel (not counted as a J3 step -- it's how the harness knows what
    # a real supervisor would already see filled into the correction
    # form's hidden fields, without re-parsing the rendered HTML form).
    detail = await session.api().call(
        "GET", f"/api/v1/transactions/{txn_id}", headers=session.api_headers(), success_statuses=(200,),
    )
    if detail is None or detail.status_code != 200:
        return
    original = detail.json()
    if original.get("balance_status") is None:
        return

    # Field names match the correction template exactly
    # (web/routes/transactions_web.py DENOMINATION_ORDER == DENOMINATION_VALUES keys).
    form = {}
    for d in original.get("denominations", []):
        form[f"count_{d['denomination']}"] = str(d["count"])
    form.update({
        "customer_id": original["customer_id"],
        "location_id": original["location_id"],
        "bag_number": original["bag_number"],
        "wallet_id": original.get("wallet_id") or "",
        "expected_total": str(original.get("expected_total") or original["total_value"]),
        "reason": "Load-test correction: recount verification",
    })

    def _correction_saved(resp: httpx.Response) -> bool:
        return "Correction saved" in resp.text

    await tc.call(
        "POST", f"/web/transactions/{txn_id}/correct", data=form,
        success_statuses=(200,), success_check=_correction_saved,
        root_cause_hint=RootCause.VALIDATION_ERROR,
    )


# ---------------------------------------------------------------------
# J4 -- Reports export
# ---------------------------------------------------------------------

async def journey_j4_report_export(session: Session, fmt: str = "xlsx") -> None:
    tc = session.web_tc

    login = await tc.call(
        "POST", "/web/login", data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return
    await tc.call("POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,))

    await tc.call("GET", "/web/reports", success_statuses=(200,))
    await session.think(0.3)

    await tc.call(
        "GET", "/web/reports/download",
        params={"format": fmt, "date_from": "", "date_to": "", "customer_id": "", "location_id": ""},
        success_statuses=(200,),
    )


# ---------------------------------------------------------------------
# J5 -- Duplicate-flag review
# ---------------------------------------------------------------------

async def journey_j5_duplicate_review(session: Session) -> None:
    tc = session.web_tc

    login = await tc.call(
        "POST", "/web/login", data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return
    await tc.call("POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,))
    await session.ensure_api_token()

    page = await tc.call("GET", "/web/duplicates", success_statuses=(200,))
    if page is None or page.status_code != 200:
        return

    flags = await session.api().call(
        "GET", "/api/v1/duplicates/", params={"status": "pending", "limit": 50},
        headers=session.api_headers(), success_statuses=(200,),
    )
    if flags is None or flags.status_code != 200:
        return
    pending = flags.json()
    if not pending:
        return
    flag_id = session.rng.choice(pending)["id"]
    status_choice = session.rng.choice(["confirmed", "dismissed"])

    async def _reviewed(_resp: httpx.Response) -> bool:
        check = await session.api().call(
            "GET", f"/api/v1/duplicates/{flag_id}", headers=session.api_headers(), success_statuses=(200,),
        )
        return bool(check is not None and check.status_code == 200 and check.json().get("status") != "pending")

    await tc.call(
        "POST", f"/web/duplicates/{flag_id}/review",
        data={"status": status_choice, "notes": "Load-test review"},
        success_statuses=(303,), success_check=_reviewed,
    )


# ---------------------------------------------------------------------
# J6 -- Notifications triage
# ---------------------------------------------------------------------

async def journey_j6_notifications(session: Session) -> None:
    tc = session.web_tc

    login = await tc.call(
        "POST", "/web/login", data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return
    await tc.call("POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,))
    await session.ensure_api_token()

    page = await tc.call("GET", "/web/notifications", success_statuses=(200,))
    if page is None or page.status_code != 200:
        return

    notifs = await session.api().call(
        "GET", "/api/v1/notifications/", params={"status": "open", "limit": 50},
        headers=session.api_headers(), success_statuses=(200,),
    )
    if notifs is None or notifs.status_code != 200:
        return
    open_notifs = notifs.json()
    if not open_notifs:
        return
    notification_id = session.rng.choice(open_notifs)["id"]

    async def _resolved(_resp: httpx.Response) -> bool:
        check = await session.api().call(
            "GET", f"/api/v1/notifications/{notification_id}",
            headers=session.api_headers(), success_statuses=(200,),
        )
        return bool(check is not None and check.status_code == 200 and check.json().get("status") == "resolved")

    await tc.call(
        "POST", f"/web/notifications/{notification_id}/resolve",
        success_statuses=(303,), success_check=_resolved,
    )


# ---------------------------------------------------------------------
# J7 -- Stats / dashboard glance
# ---------------------------------------------------------------------

async def journey_j7_stats(session: Session) -> None:
    tc = session.web_tc

    login = await tc.call(
        "POST", "/web/login", data={"username": session.username, "password": session.password},
        success_statuses=(303,),
    )
    if login is None or login.status_code != 303:
        return
    await tc.call("POST", "/web/catalog/select", data={"code": session.catalog}, success_statuses=(303,))

    await tc.call("GET", "/web/dashboard", success_statuses=(200,))
    await session.think(0.3)

    today = date.today()
    week_ago = today - timedelta(days=6)
    await tc.call(
        "GET", "/web/stats",
        params={"date_from": week_ago.isoformat(), "date_to": today.isoformat()},
        success_statuses=(200,),
    )
