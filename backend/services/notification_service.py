import uuid
import json
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.notification_repository import NotificationRepository
from ..models.notification import Notification


class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = NotificationRepository(db)

    async def raise_event(
        self,
        event_type: str,
        severity: str,
        message: str,
        related_transaction_id: Optional[uuid.UUID] = None,
        related_user_id: Optional[uuid.UUID] = None,
        details: Optional[dict] = None,
    ) -> Notification:
        return await self.repo.create(
            event_type=event_type,
            severity=severity,
            message=message,
            related_transaction_id=related_transaction_id,
            related_user_id=related_user_id,
            details_json=json.dumps(details) if details else None,
        )

    async def list_open(self, limit: int = 100) -> list[Notification]:
        return await self.repo.list_filtered(status="open", limit=limit)

    async def list_notifications(
        self, status: Optional[str] = None, event_type: Optional[str] = None, limit: int = 100
    ) -> list[Notification]:
        return await self.repo.list_filtered(status=status, event_type=event_type, limit=limit)

    async def count_open(self) -> int:
        return await self.repo.count_open()

    async def get_notification(self, notification_id: uuid.UUID) -> Optional[Notification]:
        return await self.repo.get_by_id(notification_id)

    async def resolve(
        self, notification_id: uuid.UUID, status: str, resolved_by_user_id: uuid.UUID, notes: Optional[str] = None
    ) -> Notification:
        notification = await self.repo.set_status(notification_id, status, resolved_by_user_id, notes)
        if not notification:
            raise ValueError(f"Notification {notification_id} not found")
        return notification
