import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, date
from typing import Annotated, Optional
from fastapi import APIRouter, Request, Depends, Form
from backend.core.catalogs import CatalogCode
from backend.services.transaction_service import TransactionService, DENOMINATION_VALUES
from backend.services.auth_service import AuthService
from backend.schemas.transaction import TransactionCreate, DenominationEntry
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebAdminOnly, WebCatalogDB, WebCoreDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-transactions"])

DENOMINATION_ORDER = list(DENOMINATION_VALUES.keys())


async def _transactions_list_context(
    current_user,
    db,
    core_db,
    catalog_code: CatalogCode,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_id: Optional[str] = None,
    location_id: Optional[str] = None,
    error_message: Optional[str] = None,
    success_message: Optional[str] = None,
) -> dict:
    service = TransactionService(db)

    parsed_from = datetime.fromisoformat(date_from) if date_from else None
    parsed_to = datetime.fromisoformat(date_to + "T23:59:59") if date_to else None

    transactions = await service.list_transactions(
        date_from=parsed_from,
        date_to=parsed_to,
        customer_id=customer_id or None,
        location_id=location_id or None,
        limit=500,
    )
    unbalanced_count = sum(1 for t in transactions if t.balance_status.value == "NOT BALANCED")
    is_admin = current_user.role.value == "administrator"

    cashiers = []
    if is_admin:
        cashiers = sorted(await AuthService(core_db).list_users(), key=lambda u: u.username.lower())

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "transactions": transactions,
        "unbalanced_count": unbalanced_count,
        "is_admin": is_admin,
        "cashiers": cashiers,
        "filters": {
            "date_from": date_from or "",
            "date_to": date_to or "",
            "customer_id": customer_id or "",
            "location_id": location_id or "",
        },
        "error_message": error_message,
        "success_message": success_message,
    })
    return context


@router.get("/transactions")
async def list_transactions(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    core_db: WebCoreDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_id: Optional[str] = None,
    location_id: Optional[str] = None,
):
    context = await _transactions_list_context(
        current_user, db, core_db, catalog_code,
        date_from=date_from, date_to=date_to, customer_id=customer_id, location_id=location_id,
    )
    return templates.TemplateResponse(request, "transactions.html", context)


@router.post("/transactions/bulk-transfer")
async def bulk_transfer_transactions(
    request: Request,
    current_user: WebAdminOnly,
    db: WebCatalogDB,
    core_db: WebCoreDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    transaction_ids: list[str] = Form(...),
    new_business_date: date = Form(...),
    reason: str = Form(...),
):
    service = TransactionService(db)
    error_message = None
    success_message = None
    try:
        ids = [uuid.UUID(tid) for tid in transaction_ids]
        result = await service.bulk_transfer_business_date(ids, new_business_date, reason, current_user.id)
        success_message = f"{len(result['moved'])} transaction(s) moved to {new_business_date}."
        if result["missing"]:
            success_message += f" {len(result['missing'])} could not be found."
    except ValueError as e:
        error_message = str(e)

    context = await _transactions_list_context(
        current_user, db, core_db, catalog_code, error_message=error_message, success_message=success_message,
    )
    return templates.TemplateResponse(request, "transactions.html", context)


@router.post("/transactions/transfer-cashier")
async def transfer_cashier_transactions(
    request: Request,
    current_user: WebAdminOnly,
    db: WebCatalogDB,
    core_db: WebCoreDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    cashier_user_id: uuid.UUID = Form(...),
    business_date: date = Form(...),
    new_business_date: date = Form(...),
    reason: str = Form(...),
):
    service = TransactionService(db)
    error_message = None
    success_message = None
    try:
        result = await service.transfer_business_date_for_cashier(
            cashier_user_id, business_date, new_business_date, reason, current_user.id
        )
        success_message = f"{len(result['moved'])} transaction(s) moved from {business_date} to {new_business_date}."
    except ValueError as e:
        error_message = str(e)

    context = await _transactions_list_context(
        current_user, db, core_db, catalog_code, error_message=error_message, success_message=success_message,
    )
    return templates.TemplateResponse(request, "transactions.html", context)


@router.get("/transactions/{transaction_id}")
async def transaction_detail(
    request: Request,
    transaction_id: uuid.UUID,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = TransactionService(db)
    txn = await service.get_transaction(transaction_id)
    corrections = await service.list_corrections(transaction_id) if txn else []
    original = (
        await service.get_transaction(txn.original_transaction_id)
        if txn and txn.original_transaction_id else None
    )

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "txn": txn,
        "corrections": corrections,
        "original": original,
        "is_admin": current_user.role.value == "administrator",
        "can_correct": bool(txn) and not txn.is_superseded and not txn.original_transaction_id,
    })
    return templates.TemplateResponse(request, "transaction_detail.html", context)


@router.post("/transactions/{transaction_id}/transfer")
async def transfer_transaction(
    request: Request,
    transaction_id: uuid.UUID,
    current_user: WebAdminOnly,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    new_business_date: date = Form(...),
    reason: str = Form(...),
):
    service = TransactionService(db)
    error_message = None
    success_message = None
    try:
        await service.transfer_business_date(transaction_id, new_business_date, reason, current_user.id)
        success_message = f"Transaction moved to {new_business_date}."
    except ValueError as e:
        error_message = str(e)

    txn = await service.get_transaction(transaction_id)
    corrections = await service.list_corrections(transaction_id) if txn else []
    original = (
        await service.get_transaction(txn.original_transaction_id)
        if txn and txn.original_transaction_id else None
    )
    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "txn": txn,
        "corrections": corrections,
        "original": original,
        "is_admin": True,
        "can_correct": bool(txn) and not txn.is_superseded and not txn.original_transaction_id,
        "error_message": error_message,
        "success_message": success_message,
    })
    return templates.TemplateResponse(request, "transaction_detail.html", context)


def _denomination_prefill(txn) -> list[tuple[str, int]]:
    counts = {d.denomination: d.count for d in (txn.denominations if txn else [])}
    return [(label, counts.get(label, 0)) for label in DENOMINATION_ORDER]


@router.get("/transactions/{transaction_id}/correct")
async def correct_transaction_form(
    request: Request,
    transaction_id: uuid.UUID,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = TransactionService(db)
    txn = await service.get_transaction(transaction_id)
    error_message = None
    if not txn:
        error_message = "Transaction not found."
    elif txn.is_superseded:
        error_message = "This transaction has already been corrected."
    elif txn.original_transaction_id:
        error_message = "This transaction is itself a correction — correct the original instead."

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "txn": txn,
        "denominations": _denomination_prefill(txn),
        "show_form": bool(txn) and not txn.is_superseded and not txn.original_transaction_id,
        "error_message": error_message,
    })
    return templates.TemplateResponse(request, "transaction_correct.html", context)


@router.post("/transactions/{transaction_id}/correct")
async def correct_transaction_submit(
    request: Request,
    transaction_id: uuid.UUID,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = TransactionService(db)
    txn = await service.get_transaction(transaction_id)
    form = await request.form()

    error_message = None
    corrected = None
    try:
        denominations = []
        total_value = Decimal("0")
        for label in DENOMINATION_ORDER:
            raw_count = form.get(f"count_{label}", "0")
            count = int(raw_count) if raw_count else 0
            value = DENOMINATION_VALUES[label] * count
            denominations.append(DenominationEntry(denomination=label, count=count, value=value))
            total_value += value

        raw_expected = (form.get("expected_total") or "").strip()
        expected_total = Decimal(raw_expected) if raw_expected else None

        reason = form.get("reason", "").strip()
        if not reason:
            raise ValueError("A reason is required to correct a transaction")

        data = TransactionCreate(
            customer_id=form.get("customer_id", ""),
            location_id=form.get("location_id", ""),
            bag_number=form.get("bag_number", ""),
            wallet_id=(form.get("wallet_id") or None),
            total_value=total_value,
            expected_total=expected_total,
            denominations=denominations,
        )
        corrected = await service.correct_transaction(transaction_id, data, reason, current_user.id)
    except (ValueError, InvalidOperation) as e:
        error_message = str(e)

    if corrected:
        return templates.TemplateResponse(
            request, "transaction_detail.html",
            {
                **(await build_nav_context(current_user, catalog_code, db)),
                "active_nav": "transactions",
                "txn": corrected,
                "corrections": [],
                "original": await service.get_transaction(transaction_id),
                "is_admin": current_user.role.value == "administrator",
                "can_correct": False,
                "success_message": "Correction saved.",
            },
        )

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "txn": txn,
        "denominations": _denomination_prefill(txn),
        "show_form": bool(txn) and not txn.is_superseded and not txn.original_transaction_id,
        "error_message": error_message,
    })
    return templates.TemplateResponse(request, "transaction_correct.html", context)
