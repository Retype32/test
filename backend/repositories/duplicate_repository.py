import uuid
import json
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.duplicate import DuplicateFlag, DuplicateFlagStatus


class DuplicateRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        transaction_id: uuid.UUID,
        duplicate_of_transaction_id: uuid.UUID,
        matched_fields: list[str],
        notification_id: Optional[uuid.UUID] = None,
    ) -> DuplicateFlag:
        flag = DuplicateFlag(
            transaction_id=transaction_id,
            duplicate_of_transaction_id=duplicate_of_transaction_id,
            matched_fields=json.dumps(matched_fields),
            notification_id=notification_id,
        )
        self.db.add(flag)
        await self.db.flush()
        await self.db.refresh(flag)
        return flag

    async def get_by_id(self, flag_id: uuid.UUID) -> Optional[DuplicateFlag]:
        result = await self.db.execute(select(DuplicateFlag).where(DuplicateFlag.id == flag_id))
        return result.scalar_one_or_none()

    async def list_filtered(self, status: Optional[str] = None, limit: int = 100) -> list[DuplicateFlag]:
        query = select(DuplicateFlag).order_by(DuplicateFlag.detected_at.desc())
        if status:
            query = query.where(DuplicateFlag.status == status)
        query = query.limit(limit)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_confirmed_for_user_transactions(self, transaction_ids: list[uuid.UUID]) -> int:
        if not transaction_ids:
            return 0
        result = await self.db.execute(
            select(DuplicateFlag).where(
                DuplicateFlag.status == DuplicateFlagStatus.confirmed,
                DuplicateFlag.transaction_id.in_(transaction_ids),
            )
        )
        return len(result.scalars().all())

    async def set_status(
        self, flag_id: uuid.UUID, status: str, reviewed_by_user_id: uuid.UUID
    ) -> Optional[DuplicateFlag]:
        flag = await self.get_by_id(flag_id)
        if not flag:
            return None
        flag.status = status
        flag.reviewed_by_user_id = reviewed_by_user_id
        flag.reviewed_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(flag)
        return flag
