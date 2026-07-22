import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..models.notification import Notification, NotificationStatus


class NotificationRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        event_type: str,
        severity: str,
        message: str,
        related_transaction_id: Optional[uuid.UUID] = None,
        related_user_id: Optional[uuid.UUID] = None,
        details_json: Optional[str] = None,
    ) -> Notification:
        notification = Notification(
            event_type=event_type,
            severity=severity,
            message=message,
            related_transaction_id=related_transaction_id,
            related_user_id=related_user_id,
            details_json=details_json,
        )
        self.db.add(notification)
        await self.db.flush()
        await self.db.refresh(notification)
        return notification

    async def get_by_id(self, notification_id: uuid.UUID) -> Optional[Notification]:
        result = await self.db.execute(select(Notification).where(Notification.id == notification_id))
        return result.scalar_one_or_none()

    async def list_filtered(
        self,
        status: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Notification]:
        query = select(Notification).order_by(Notification.created_at.desc())
        if status:
            query = query.where(Notification.status == status)
        if event_type:
            query = query.where(Notification.event_type == event_type)
        query = query.limit(limit).offset(offset)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_open(self) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(Notification).where(Notification.status == NotificationStatus.open)
        )
        return result.scalar_one()

    async def set_status(
        self,
        notification_id: uuid.UUID,
        status: str,
        resolved_by_user_id: Optional[uuid.UUID] = None,
        notes: Optional[str] = None,
    ) -> Optional[Notification]:
        notification = await self.get_by_id(notification_id)
        if not notification:
            return None
        notification.status = status
        notification.resolved_by_user_id = resolved_by_user_id
        notification.resolved_at = datetime.now(timezone.utc)
        notification.resolution_notes = notes
        await self.db.flush()
        await self.db.refresh(notification)
        return notification
