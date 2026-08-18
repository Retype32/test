import os
import tempfile
from datetime import datetime
from typing import Annotated, Optional
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from backend.core.catalogs import CatalogCode
from backend.services.transaction_service import TransactionService
from backend.services.customer_service import CustomerService
from reports.report_engine import build_transactions_excel, build_transactions_csv
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-reports"])


@router.get("/reports")
async def reports_form(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    context = await build_nav_context(current_user, catalog_code, db)
    context["active_nav"] = "reports"
    return templates.TemplateResponse(request, "reports.html", context)


async def _fetch_enriched_transactions(
    db, date_from: Optional[str], date_to: Optional[str], customer_id: Optional[str], location_id: Optional[str]
) -> list[dict]:
    txn_service = TransactionService(db)
    parsed_from = datetime.fromisoformat(date_from) if date_from else None
    parsed_to = datetime.fromisoformat(date_to + "T23:59:59") if date_to else None

    transactions = await txn_service.list_transactions(
        date_from=parsed_from, date_to=parsed_to,
        customer_id=customer_id or None, location_id=location_id or None,
        limit=5000,
    )

    customer_service = CustomerService(db)
    customers = await customer_service.list_customers()
    customer_names = {c.customer_id: c.customer_name for c in customers}
    location_names = {loc.location_id: loc.location_name for c in customers for loc in c.locations}

    return [
        {
            "created_at": t.created_at,
            "customer_id": t.customer_id,
            "customer_name": customer_names.get(t.customer_id, t.customer_id),
            "location_name": location_names.get(t.location_id, t.location_id),
            "bag_number": t.bag_number,
            "wallet_id": t.wallet_id,
            "username": t.username,
            "expected_total": t.expected_total,
            "total_value": t.total_value,
            "denominations": [
                {"denomination": d.denomination, "count": d.count, "value": d.value}
                for d in t.denominations
            ],
        }
        for t in transactions
    ]


@router.get("/reports/download")
async def download_report(
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    format: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    customer_id: Optional[str] = None,
    location_id: Optional[str] = None,
):
    if format not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail="format must be 'xlsx' or 'csv'")

    enriched = await _fetch_enriched_transactions(db, date_from, date_to, customer_id, location_id)

    suffix = ".xlsx" if format == "xlsx" else ".csv"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    if format == "xlsx":
        build_transactions_excel(enriched, tmp.name)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        build_transactions_csv(enriched, tmp.name)
        media_type = "text/csv"

    filename = f"transactions_{catalog_code.value}_{datetime.now().strftime('%Y%m%d')}{suffix}"
    return FileResponse(
        tmp.name,
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(os.unlink, tmp.name),
    )
