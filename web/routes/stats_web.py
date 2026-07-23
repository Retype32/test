from datetime import datetime, timedelta
from typing import Annotated, Optional
from fastapi import APIRouter, Request, Depends
from backend.core.catalogs import CatalogCode
from backend.services.stats_service import StatsService
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-stats"])


@router.get("/stats")
async def stats_page(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    today = datetime.now().date()
    parsed_from = datetime.fromisoformat(date_from).date() if date_from else today - timedelta(days=6)
    parsed_to = datetime.fromisoformat(date_to).date() if date_to else today

    service = StatsService(db)
    rows = await service.get_processor_stats(parsed_from, parsed_to)

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "stats",
        "rows": rows,
        "filters": {"date_from": parsed_from.isoformat(), "date_to": parsed_to.isoformat()},
    })
    return templates.TemplateResponse(request, "stats.html", context)
