from datetime import date, datetime, timedelta
from typing import Annotated
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from backend.core.catalogs import CatalogCode
from backend.services.eod_service import EODService
from backend.core.security import require_csrf
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebAdminOnly, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-eod"])


@router.get("/eod")
async def eod_page(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = EODService(db)
    today = datetime.now().date()
    closures = await service.list_closures(date_from=today - timedelta(days=60), date_to=today)

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({
        "active_nav": "eod",
        "today": today,
        "closures": closures,
        "is_admin": current_user.role.value == "administrator",
    })
    return templates.TemplateResponse(request, "eod.html", context)


@router.post("/eod/close")
async def close_day(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    _csrf: Annotated[None, Depends(require_csrf)],
    business_date: date = Form(...),
):
    service = EODService(db)
    try:
        await service.close_day(business_date, current_user.id)
    except ValueError:
        pass  # already closed — the eod list will show its current state either way
    return RedirectResponse("/web/eod", status_code=303)


@router.post("/eod/reopen")
async def reopen_day(
    request: Request,
    current_user: WebAdminOnly,
    db: WebCatalogDB,
    _csrf: Annotated[None, Depends(require_csrf)],
    business_date: date = Form(...),
    reason: str = Form(...),
):
    service = EODService(db)
    try:
        await service.reopen_day(business_date, current_user.id, reason)
    except ValueError:
        pass
    return RedirectResponse("/web/eod", status_code=303)
