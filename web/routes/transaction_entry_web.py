import asyncio
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse
from backend.core.catalogs import CatalogCode
from backend.services.customer_service import CustomerService
from backend.services.transaction_service import TransactionService, DENOMINATION_VALUES
from backend.schemas.transaction import TransactionCreate, DenominationEntry
from ..templating import templates
from ..deps import WebCurrentUser, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-transaction-entry"])

# Server-side session key holding the transaction currently being built across
# the wizard's four popup screens. Each screen only ever adds to this dict, so
# a step re-reads whatever earlier steps already collected instead of every
# page re-posting the whole thing as hidden fields (separate windows/pages
# can't share form state, but they do share the session cookie).
DRAFT_KEY = "txn_draft"

# Safe (non-€) form field name <-> denomination label used everywhere else
# in the app (TransactionService.DENOMINATION_VALUES, the report engine).
DENOM_FIELDS: list[tuple[str, str]] = [
    ("count_500", "€500"),
    ("count_200", "€200"),
    ("count_100", "€100"),
    ("count_50", "€50"),
    ("count_20", "€20"),
    ("count_10", "€10"),
    ("count_5", "€5"),
    ("count_coins", "Coins"),
]


def _draft(request: Request) -> dict:
    return dict(request.session.get(DRAFT_KEY, {}))


def _save_draft(request: Request, draft: dict) -> None:
    # Session values are JSON-serialised under the hood, so nothing Decimal
    # ever goes in here directly -- amounts are kept as strings and only
    # turned back into Decimal where they're used.
    request.session[DRAFT_KEY] = draft


def _build_denominations(form: dict) -> tuple[list[dict], Decimal, list[str]]:
    denoms = []
    total = Decimal("0")
    errors = []
    for field, label in DENOM_FIELDS:
        raw = (form.get(field) or "").strip()
        if not raw:
            continue
        try:
            count = int(raw)
        except ValueError:
            errors.append(f"{label}: enter a whole number.")
            continue
        if count < 0:
            errors.append(f"{label}: cannot be negative.")
            continue
        if count > 0:
            value = DENOMINATION_VALUES[label] * count
            total += value
            denoms.append({"denomination": label, "count": count, "value": value})
    return denoms, total, errors


def _validate_bag_number(raw: str) -> tuple[str | None, str | None]:
    normalized = re.sub(r"[\s-]", "", raw or "")
    if not re.fullmatch(r"\d{12,16}", normalized):
        return None, "Bag number must be 12-16 digits (scan the bag's barcode)."
    return normalized, None


def _validate_amount(raw: str) -> tuple[Decimal | None, str | None]:
    try:
        declared = Decimal((raw or "").strip().replace(",", "."))
        if declared < 0:
            raise InvalidOperation
    except InvalidOperation:
        return None, "Enter a valid amount (0 or more)."
    return declared, None


def _validate_wallet_id(raw: str) -> tuple[str | None, str | None]:
    wallet_id = (raw or "").strip()
    if not wallet_id:
        return None, "Scan or enter the Wallet ID."
    if len(wallet_id) > 50:
        return None, "Wallet ID is too long."
    return wallet_id, None


@router.get("/transactions/new")
async def new_transaction_form(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    context = await build_nav_context(current_user, catalog_code, db)
    context.update({"active_nav": "new_transaction"})
    return templates.TemplateResponse(request, "transaction_new.html", context)


# --------------------------------------------------------------------------
# Wizard step 1 -- Customer & Location
# --------------------------------------------------------------------------

@router.get("/transactions/new/wizard/customer")
async def wizard_customer_form(request: Request, current_user: WebCurrentUser):
    # Every trip through the popup's first screen starts a brand new
    # transaction, so any previous in-progress draft is discarded here.
    _save_draft(request, {})
    return templates.TemplateResponse(request, "wizard_customer.html", {
        "user": current_user, "step": 1, "error_message": None,
    })


@router.get("/transactions/new/wizard/customer-lookup")
async def wizard_customer_lookup(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    customer_id: str = "",
):
    customer_id = customer_id.strip()
    if not customer_id:
        return JSONResponse({"found": False, "error": "Enter a Customer ID."})

    customer = await CustomerService(db).validate_customer(customer_id)
    if not customer:
        return JSONResponse({"found": False, "error": f"No customer found for ID {customer_id}."})

    return JSONResponse({
        "found": True,
        "customer_id": customer.customer_id,
        "customer_name": customer.customer_name,
        "locations": [
            {"location_id": l.location_id, "location_name": l.location_name}
            for l in customer.locations
        ],
    })


@router.post("/transactions/new/wizard/customer")
async def wizard_customer_submit(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    customer_id: str = Form(...),
    location_id: str = Form(...),
):
    customer_id = customer_id.strip()
    location_id = location_id.strip()

    customer_service = CustomerService(db)
    customer = await customer_service.validate_customer(customer_id)
    if not customer:
        return templates.TemplateResponse(request, "wizard_customer.html", {
            "user": current_user, "step": 1,
            "error_message": f"Customer {customer_id} not found.",
        }, status_code=400)

    location = await customer_service.get_location(location_id)
    if not location or location.customer_id != customer_id:
        return templates.TemplateResponse(request, "wizard_customer.html", {
            "user": current_user, "step": 1,
            "error_message": f"Location '{location_id}' not found for this customer.",
        }, status_code=400)

    _save_draft(request, {
        "customer_id": customer.customer_id,
        "customer_name": customer.customer_name,
        "location_id": location.location_id,
        "location_name": location.location_name,
    })
    return RedirectResponse("/web/transactions/new/wizard/bag", status_code=303)


# --------------------------------------------------------------------------
# Wizard step 2 -- Bag Number & Declared Amount
# --------------------------------------------------------------------------

@router.get("/transactions/new/wizard/bag")
async def wizard_bag_form(request: Request, current_user: WebCurrentUser):
    draft = _draft(request)
    if not draft.get("customer_id") or not draft.get("location_id"):
        return RedirectResponse("/web/transactions/new/wizard/customer", status_code=303)
    return templates.TemplateResponse(request, "wizard_bag.html", {
        "user": current_user, "step": 2, "draft": draft, "error_message": None,
        "form_values": {},
    })


@router.post("/transactions/new/wizard/bag")
async def wizard_bag_submit(
    request: Request,
    current_user: WebCurrentUser,
    bag_number: str = Form(...),
    amount: str = Form(""),
):
    draft = _draft(request)
    if not draft.get("customer_id") or not draft.get("location_id"):
        return RedirectResponse("/web/transactions/new/wizard/customer", status_code=303)

    async def _error(message: str):
        return templates.TemplateResponse(request, "wizard_bag.html", {
            "user": current_user, "step": 2, "draft": draft, "error_message": message,
            "form_values": {"bag_number": bag_number, "amount": amount},
        }, status_code=400)

    normalized_bag, error = _validate_bag_number(bag_number)
    if error:
        return await _error(error)

    declared, error = _validate_amount(amount)
    if error:
        return await _error(error)

    draft["bag_number"] = normalized_bag
    draft["expected_total"] = str(declared)
    _save_draft(request, draft)
    return RedirectResponse("/web/transactions/new/wizard/wallet", status_code=303)


# --------------------------------------------------------------------------
# Wizard step 3 -- Wallet ID
# --------------------------------------------------------------------------

@router.get("/transactions/new/wizard/wallet")
async def wizard_wallet_form(request: Request, current_user: WebCurrentUser):
    draft = _draft(request)
    if not draft.get("bag_number"):
        return RedirectResponse("/web/transactions/new/wizard/bag", status_code=303)
    return templates.TemplateResponse(request, "wizard_wallet.html", {
        "user": current_user, "step": 3, "draft": draft, "error_message": None,
    })


@router.post("/transactions/new/wizard/wallet")
async def wizard_wallet_submit(
    request: Request,
    current_user: WebCurrentUser,
    wallet_id: str = Form(...),
):
    draft = _draft(request)
    if not draft.get("bag_number"):
        return RedirectResponse("/web/transactions/new/wizard/bag", status_code=303)

    validated_wallet, error = _validate_wallet_id(wallet_id)
    if error:
        return templates.TemplateResponse(request, "wizard_wallet.html", {
            "user": current_user, "step": 3, "draft": draft,
            "error_message": error,
        }, status_code=400)

    draft["wallet_id"] = validated_wallet
    _save_draft(request, draft)
    return RedirectResponse("/web/transactions/new/wizard/cash", status_code=303)


# --------------------------------------------------------------------------
# Wizard step 4 -- Cash count -> confirm -> complete
# --------------------------------------------------------------------------

@router.get("/transactions/new/wizard/cash")
async def wizard_cash_form(request: Request, current_user: WebCurrentUser):
    draft = _draft(request)
    if not draft.get("wallet_id"):
        return RedirectResponse("/web/transactions/new/wizard/wallet", status_code=303)
    return templates.TemplateResponse(request, "wizard_cash.html", {
        "user": current_user, "step": 4, "draft": draft, "error_message": None,
        "denom_fields": DENOM_FIELDS, "form_values": {},
    })


@router.post("/transactions/new/wizard/cash")
async def wizard_cash_submit(
    request: Request,
    current_user: WebCurrentUser,
    count_500: str = Form("0"),
    count_200: str = Form("0"),
    count_100: str = Form("0"),
    count_50: str = Form("0"),
    count_20: str = Form("0"),
    count_10: str = Form("0"),
    count_5: str = Form("0"),
    count_coins: str = Form("0"),
):
    draft = _draft(request)
    if not draft.get("wallet_id"):
        return RedirectResponse("/web/transactions/new/wizard/wallet", status_code=303)

    form_values = {
        "count_500": count_500, "count_200": count_200, "count_100": count_100,
        "count_50": count_50, "count_20": count_20, "count_10": count_10,
        "count_5": count_5, "count_coins": count_coins,
    }
    denoms, total, errors = _build_denominations(form_values)
    if errors:
        return templates.TemplateResponse(request, "wizard_cash.html", {
            "user": current_user, "step": 4, "draft": draft,
            "error_message": " ".join(errors),
            "denom_fields": DENOM_FIELDS, "form_values": form_values,
        }, status_code=400)

    draft["denominations"] = [{"denomination": d["denomination"], "count": d["count"],
                                "value": str(d["value"])} for d in denoms]
    draft["total_value"] = str(total)
    # C-1: minted fresh every time this confirm screen is (re-)rendered from
    # the cash-count step -- carried through resubmits (a double-click on
    # "Accept Transaction" posts the same rendered page, same nonce), but a
    # genuine edit-and-recount loop back through this same POST gets a new
    # one, same as a page refresh would.
    draft["idempotency_nonce"] = uuid.uuid4().hex
    _save_draft(request, draft)

    expected = Decimal(draft["expected_total"])
    diff = total - expected
    diff_status = "BALANCED" if diff == 0 else ("SHORTAGE" if diff < 0 else "OVERAGE")

    return templates.TemplateResponse(request, "wizard_confirm.html", {
        "user": current_user, "step": 4, "draft": draft, "error_message": None,
        "denominations": denoms, "total": total, "expected": expected,
        "diff": diff, "diff_status": diff_status,
    })


@router.post("/transactions/new/wizard/complete")
async def wizard_complete(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    idempotency_nonce: str = Form(""),
):
    draft = _draft(request)
    if "denominations" not in draft or "total_value" not in draft:
        return RedirectResponse("/web/transactions/new/wizard/cash", status_code=303)

    expected = Decimal(draft["expected_total"])
    total = Decimal(draft["total_value"])
    denoms = draft["denominations"]

    service = TransactionService(db)
    data = TransactionCreate(
        customer_id=draft["customer_id"],
        location_id=draft["location_id"],
        bag_number=draft["bag_number"],
        wallet_id=draft["wallet_id"],
        total_value=total,
        expected_total=expected,
        denominations=[DenominationEntry(denomination=d["denomination"], count=d["count"],
                                          value=Decimal(d["value"])) for d in denoms],
    )

    diff = total - expected
    diff_status = "BALANCED" if diff == 0 else ("SHORTAGE" if diff < 0 else "OVERAGE")

    # C-1: the hidden field on wizard_confirm.html, if this browser session
    # rendered that page after this feature shipped -- absent (older
    # session state, or a non-browser POST) falls back to today's
    # unprotected behavior exactly, same as create_transaction with no
    # header at all.
    idempotency_key = (idempotency_nonce or draft.get("idempotency_nonce") or "").strip() or None

    try:
        txn = await service.create_transaction(
            current_user.id, current_user.username, data,
            idempotency_key=idempotency_key, catalog=catalog_code.value,
        )
    except ValueError as exc:
        return templates.TemplateResponse(request, "wizard_confirm.html", {
            "user": current_user, "step": 4, "draft": draft,
            "error_message": str(exc),
            "denominations": [{"denomination": d["denomination"], "count": d["count"],
                                "value": Decimal(d["value"])} for d in denoms],
            "total": total, "expected": expected, "diff": diff, "diff_status": diff_status,
        }, status_code=400)

    # Keep the customer/location/bag context around (drop only the
    # per-wallet fields) so "Complete Transaction" on the result screen can
    # start another, fully independent transaction for the same bag without
    # re-scanning the customer or bag number.
    _save_draft(request, {
        "customer_id": draft["customer_id"],
        "customer_name": draft["customer_name"],
        "location_id": draft["location_id"],
        "location_name": draft["location_name"],
        "bag_number": draft["bag_number"],
    })

    return templates.TemplateResponse(request, "wizard_result.html", {
        "user": current_user, "step": 4, "txn": txn,
        "expected_total": expected, "diff": diff, "diff_status": diff_status,
    })


@router.get("/transactions/new/wizard/cancel")
async def wizard_cancel(request: Request, current_user: WebCurrentUser):
    _save_draft(request, {})
    return templates.TemplateResponse(request, "wizard_cancelled.html", {"user": current_user, "step": 0})


# --------------------------------------------------------------------------
# Same bag, next wallet -- picks up right after a transaction completes.
# Each wallet still becomes its own fully independent Transaction row (same
# customer/location/bag_number, own wallet_id/expected_total/denominations),
# so a mistake on one wallet never touches the others.
# --------------------------------------------------------------------------

@router.get("/transactions/new/wizard/wallet/next")
async def wizard_next_wallet_form(request: Request, current_user: WebCurrentUser):
    draft = _draft(request)
    if not draft.get("bag_number"):
        return RedirectResponse("/web/transactions/new/wizard/customer", status_code=303)
    return templates.TemplateResponse(request, "wizard_wallet_next.html", {
        "user": current_user, "step": 3, "draft": draft, "error_message": None,
        "form_values": {},
    })


@router.post("/transactions/new/wizard/wallet/next")
async def wizard_next_wallet_submit(
    request: Request,
    current_user: WebCurrentUser,
    wallet_id: str = Form(...),
    amount: str = Form(""),
):
    draft = _draft(request)
    if not draft.get("bag_number"):
        return RedirectResponse("/web/transactions/new/wizard/customer", status_code=303)

    async def _error(message: str):
        return templates.TemplateResponse(request, "wizard_wallet_next.html", {
            "user": current_user, "step": 3, "draft": draft, "error_message": message,
            "form_values": {"wallet_id": wallet_id, "amount": amount},
        }, status_code=400)

    validated_wallet, error = _validate_wallet_id(wallet_id)
    if error:
        return await _error(error)

    declared, error = _validate_amount(amount)
    if error:
        return await _error(error)

    draft["wallet_id"] = validated_wallet
    draft["expected_total"] = str(declared)
    _save_draft(request, draft)
    return RedirectResponse("/web/transactions/new/wizard/cash", status_code=303)


def _read_counter_sync() -> dict:
    from hardware import wait_for_shared_count
    return wait_for_shared_count().denominations


@router.post("/transactions/new/count")
async def read_counter(current_user: WebCurrentUser):
    try:
        denominations = await asyncio.to_thread(_read_counter_sync)
    except Exception as exc:
        return JSONResponse({"error": str(exc)})
    return JSONResponse({"denominations": denominations})
