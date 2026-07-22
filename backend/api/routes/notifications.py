import uuid
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ...services.notification_service import NotificationService
from ...schemas.notification import NotificationResponse, NotificationResolveRequest
from ..deps import SupervisorOrAbove, CatalogDB

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/", response_model=list[NotificationResponse])
async def list_notifications(
    db: CatalogDB,
    _: SupervisorOrAbove,
    status: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    service = NotificationService(db)
    notifications = await service.list_notifications(status=status, event_type=event_type, limit=limit)
    return [NotificationResponse.model_validate(n) for n in notifications]


@router.get("/unread-count")
async def unread_count(db: CatalogDB, _: SupervisorOrAbove):
    service = NotificationService(db)
    return {"count": await service.count_open()}


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(notification_id: uuid.UUID, db: CatalogDB, _: SupervisorOrAbove):
    service = NotificationService(db)
    notification = await service.get_notification(notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    return NotificationResponse.model_validate(notification)


@router.post("/{notification_id}/resolve", response_model=NotificationResponse)
async def resolve_notification(
    notification_id: uuid.UUID,
    data: NotificationResolveRequest,
    db: CatalogDB,
    current_user: SupervisorOrAbove,
):
    service = NotificationService(db)
    try:
        notification = await service.resolve(notification_id, data.status, current_user.id, data.notes)
        return NotificationResponse.model_validate(notification)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
