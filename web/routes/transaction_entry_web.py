import asyncio
import json
from decimal import Decimal, InvalidOperation
from typing import Annotated, Optional
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse
from backend.core.catalogs import CatalogCode
from backend.services.customer_service import CustomerService
from backend.services.transaction_service import TransactionService, DENOMINATION_VALUES
from backend.schemas.transaction import TransactionCreate, DenominationEntry
from ..templating import templates
from ..deps import WebCurrentUser, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-transaction-entry"])

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


def _build_denominations(form: dict) -> tuple[list[dict], Decimal]:
    denoms = []
    total = Decimal("0")
    for field, label in DENOM_FIELDS:
        try:
            count = int(form.get(field) or 0)
        except ValueError:
            count = 0
        if count > 0:
            value = DENOMINATION_VALUES[label] * count
            total += value
            denoms.append({"denomination": label, "count": count, "value": value})
    return denoms, total


async def _customers_context(db) -> tuple[list, str]:
    customers = await CustomerService(db).list_customers()
    customers_data = [
        {
            "customer_id": c.customer_id,
            "customer_name": c.customer_name,
            "locations": [
                {"location_id": l.location_id, "location_name": l.location_name}
                for l in c.locations
            ],
        }
        for c in customers
    ]
    return customers, json.dumps(customers_data)


@router.get("/transactions/new")
async def new_transaction_form(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    customers, customers_json = await _customers_context(db)
    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "new_transaction",
        "customers": customers,
        "customers_json": customers_json,
        "denom_fields": DENOM_FIELDS,
        "form_values": {},
    })
    return templates.TemplateResponse(request, "transaction_new.html", context)


@router.post("/transactions/new/confirm")
async def confirm_transaction(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    customer_id: str = Form(...),
    location_id: str = Form(...),
    bag_number: str = Form(...),
    expected_total: str = Form(""),
    count_500: str = Form("0"),
    count_200: str = Form("0"),
    count_100: str = Form("0"),
    count_50: str = Form("0"),
    count_20: str = Form("0"),
    count_10: str = Form("0"),
    count_5: str = Form("0"),
    count_coins: str = Form("0"),
):
    form_values = {
        "customer_id": customer_id, "location_id": location_id,
        "bag_number": bag_number, "expected_total": expected_total,
        "count_500": count_500, "count_200": count_200, "count_100": count_100,
        "count_50": count_50, "count_20": count_20, "count_10": count_10,
        "count_5": count_5, "count_coins": count_coins,
    }

    async def _error(message: str):
        customers, customers_json = await _customers_context(db)
        context = await build_nav_context(current_user, catalog_code, db)
        context.update({
            "active_nav": "new_transaction",
            "customers": customers,
            "customers_json": customers_json,
            "denom_fields": DENOM_FIELDS,
            "form_values": form_values,
            "error_message": message,
        })
        return templates.TemplateResponse(request, "transaction_new.html", context, status_code=400)

    customer_service = CustomerService(db)
    customer = await customer_service.validate_customer(customer_id)
    if not customer:
        return await _error(f"Customer {customer_id} not found.")
    location = await customer_service.get_location(location_id)
    if not location or location.customer_id != customer_id:
        return await _error(f"Location '{location_id}' not found for this customer.")
    if not bag_number.strip():
        return await _error("Bag number is required.")

    try:
        expected = Decimal(expected_total.strip().replace(",", ".")) if expected_total.strip() else None
        if expected is not None and expected < 0:
            raise InvalidOperation
    except InvalidOperation:
        return await _error("Enter a valid expected total (e.g. 1500.00).")

    denoms, total = _build_denominations(form_values)
    if total == 0:
        return await _error("Total cash value cannot be zero.")

    diff = None
    diff_status = None
    if expected is not None and expected > 0:
        diff = total - expected
        diff_status = "BALANCED" if diff == 0 else ("SHORTAGE" if diff < 0 else "OVERAGE")

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "new_transaction",
        "customer": customer,
        "location": location,
        "bag_number": bag_number,
        "expected_total": expected,
        "total": total,
        "diff": diff,
        "diff_status": diff_status,
        "denominations": denoms,
        "form_values": form_values,
    })
    return templates.TemplateResponse(request, "transaction_new_confirm.html", context)


@router.post("/transactions/new")
async def create_transaction(
    request: Request,
    current_user: WebCurrentUser,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    customer_id: str = Form(...),
    location_id: str = Form(...),
    bag_number: str = Form(...),
    expected_total: str = Form(""),
    count_500: str = Form("0"),
    count_200: str = Form("0"),
    count_100: str = Form("0"),
    count_50: str = Form("0"),
    count_20: str = Form("0"),
    count_10: str = Form("0"),
    count_5: str = Form("0"),
    count_coins: str = Form("0"),
):
    form_values = {
        "customer_id": customer_id, "location_id": location_id,
        "bag_number": bag_number, "expected_total": expected_total,
        "count_500": count_500, "count_200": count_200, "count_100": count_100,
        "count_50": count_50, "count_20": count_20, "count_10": count_10,
        "count_5": count_5, "count_coins": count_coins,
    }
    expected = Decimal(expected_total.strip().replace(",", ".")) if expected_total.strip() else None
    denoms, total = _build_denominations(form_values)

    service = TransactionService(db)
    data = TransactionCreate(
        customer_id=customer_id,
        location_id=location_id,
        bag_number=bag_number,
        total_value=total,
        expected_total=expected,
        denominations=[DenominationEntry(**d) for d in denoms],
    )

    try:
        txn = await service.create_transaction(current_user.id, current_user.username, data)
    except ValueError as exc:
        customers, customers_json = await _customers_context(db)
        context = await build_nav_context(current_user, catalog_code, db)
        context.update({
            "active_nav": "new_transaction",
            "customers": customers,
            "customers_json": customers_json,
            "denom_fields": DENOM_FIELDS,
            "form_values": form_values,
            "error_message": str(exc),
        })
        return templates.TemplateResponse(request, "transaction_new.html", context, status_code=400)

    diff = None
    diff_status = txn.balance_status.value
    if expected is not None and expected > 0:
        diff = txn.total_value - expected
        diff_status = "BALANCED" if diff == 0 else ("SHORTAGE" if diff < 0 else "OVERAGE")

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "new_transaction",
        "txn": txn,
        "expected_total": expected,
        "diff": diff,
        "diff_status": diff_status,
    })
    return templates.TemplateResponse(request, "transaction_new_result.html", context)


def _read_counter_sync() -> dict:
    from hardware import get_counter
    counter = get_counter()
    try:
        counter.connect()
        result = counter.wait_for_count_result()
        return result.denominations
    finally:
        try:
            counter.disconnect()
        except Exception:
            pass


@router.post("/transactions/new/count")
async def read_counter(current_user: WebCurrentUser):
    try:
        denominations = await asyncio.to_thread(_read_counter_sync)
    except Exception as exc:
        return JSONResponse({"error": str(exc)})
    return JSONResponse({"denominations": denominations})
