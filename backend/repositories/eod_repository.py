import uuid
from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from ..models.eod import EODClosure, EODStatus


class EODRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_date(self, business_date: date) -> Optional[EODClosure]:
        result = await self.db.execute(
            select(EODClosure).where(EODClosure.business_date == business_date)
        )
        return result.scalar_one_or_none()

    async def close(self, business_date: date, closed_by_user_id: uuid.UUID) -> EODClosure:
        existing = await self.get_by_date(business_date)
        if existing:
            existing.status = EODStatus.closed
            existing.closed_by_user_id = closed_by_user_id
            existing.closed_at = datetime.now(timezone.utc)
            existing.reopened_by_user_id = None
            existing.reopened_at = None
            existing.reopen_reason = None
            closure = existing
        else:
            closure = EODClosure(business_date=business_date, closed_by_user_id=closed_by_user_id)
            self.db.add(closure)

        await self.db.flush()
        await self.db.refresh(closure)
        return closure

    async def reopen(
        self, business_date: date, reopened_by_user_id: uuid.UUID, reason: Optional[str]
    ) -> Optional[EODClosure]:
        closure = await self.get_by_date(business_date)
        if not closure or closure.status != EODStatus.closed:
            return None

        closure.status = EODStatus.reopened
        closure.reopened_by_user_id = reopened_by_user_id
        closure.reopened_at = datetime.now(timezone.utc)
        closure.reopen_reason = reason
        await self.db.flush()
        await self.db.refresh(closure)
        return closure

    async def list_between(self, date_from: Optional[date], date_to: Optional[date]) -> list[EODClosure]:
        query = select(EODClosure).order_by(EODClosure.business_date.desc())
        conditions = []
        if date_from:
            conditions.append(EODClosure.business_date >= date_from)
        if date_to:
            conditions.append(EODClosure.business_date <= date_to)
        if conditions:
            query = query.where(and_(*conditions))
        result = await self.db.execute(query)
        return list(result.scalars().all())
