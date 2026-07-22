import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.core_audit import CoreAuditLog


class CoreAuditRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(self, user_id: Optional[uuid.UUID], action: str, details: Optional[str] = None):
        entry = CoreAuditLog(user_id=user_id, action=action, details=details)
        self.db.add(entry)
        await self.db.flush()

    async def list_recent(self, limit: int = 100) -> list[CoreAuditLog]:
        result = await self.db.execute(
            select(CoreAuditLog).order_by(CoreAuditLog.timestamp.desc()).limit(limit)
        )
        return list(result.scalars().all())
