import uuid
from typing import Annotated
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from backend.core.catalogs import CatalogCode
from backend.services.notification_service import NotificationService
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-notifications"])


@router.get("/notifications")
async def notifications_page(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = NotificationService(db)
    notifications = await service.list_notifications(status="open", limit=200)

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({"active_nav": "notifications", "notifications": notifications})
    return templates.TemplateResponse(request, "notifications.html", context)


@router.post("/notifications/{notification_id}/resolve")
async def resolve_notification(
    notification_id: uuid.UUID,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
):
    service = NotificationService(db)
    try:
        await service.resolve(notification_id, "resolved", current_user.id)
    except ValueError:
        pass
    return RedirectResponse("/web/notifications", status_code=303)
