from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, Request, Depends
from backend.core.catalogs import CatalogCode
from backend.services.stats_service import StatsService
from backend.services.eod_service import EODService
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-dashboard"])


@router.get("/dashboard")
async def dashboard(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    today = datetime.now().date()

    stats_service = StatsService(db)
    processor_rows = await stats_service.get_processor_stats(today, today)
    total_slips = sum(r["slip_count"] for r in processor_rows)
    total_cash = sum(r["cash_volume"] for r in processor_rows)
    total_errors = sum(r["error_count"] for r in processor_rows)

    eod_service = EODService(db)
    day_closed = await eod_service.is_day_closed(today)

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "dashboard",
        "today": today,
        "day_closed": day_closed,
        "total_slips": total_slips,
        "total_cash": total_cash,
        "total_errors": total_errors,
        "processor_count": len(processor_rows),
    })
    return templates.TemplateResponse(request, "dashboard.html", context)
