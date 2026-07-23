import uuid
from datetime import datetime, date
from typing import Annotated, Optional
from fastapi import APIRouter, Request, Depends, Form
from backend.core.catalogs import CatalogCode
from backend.services.transaction_service import TransactionService
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebAdminOnly, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-transactions"])


@router.get("/transactions")
async def list_transactions(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_id: Optional[str] = None,
    location_id: Optional[str] = None,
):
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

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "transactions": transactions,
        "filters": {
            "date_from": date_from or "",
            "date_to": date_to or "",
            "customer_id": customer_id or "",
            "location_id": location_id or "",
        },
    })
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

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "txn": txn,
        "is_admin": current_user.role.value == "administrator",
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
    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "transactions",
        "txn": txn,
        "is_admin": True,
        "error_message": error_message,
        "success_message": success_message,
    })
    return templates.TemplateResponse(request, "transaction_detail.html", context)
