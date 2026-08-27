import uuid
from datetime import date
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..models.transaction import Transaction
from ..models.duplicate import DuplicateFlag, DuplicateFlagStatus


class StatsRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def daily_activity(
        self, date_from: date, date_to: date, user_id: Optional[uuid.UUID] = None
    ) -> list[dict]:
        """One row per (user, business_date): slip count, cash volume, and the
        first/last transaction timestamp that day (used to derive an active-hours
        span for the slips/hour metric).

        Corrected-away originals are excluded. A correction leaves the original
        row in place with is_superseded=True and adds a replacement, so counting
        both would credit the processor with two slips for one bag and add the
        pre-correction amount to their cash volume on top of the corrected one --
        a correction would *raise* the recorded volume instead of restating it.
        """
        query = (
            select(
                Transaction.user_id,
                Transaction.username,
                Transaction.business_date,
                func.count().label("slip_count"),
                func.sum(Transaction.total_value).label("cash_volume"),
                func.min(Transaction.created_at).label("first_txn_at"),
                func.max(Transaction.created_at).label("last_txn_at"),
            )
            .where(Transaction.business_date >= date_from, Transaction.business_date <= date_to,
                   Transaction.is_superseded.is_not(True))
            .group_by(Transaction.user_id, Transaction.username, Transaction.business_date)
        )
        if user_id:
            query = query.where(Transaction.user_id == user_id)

        result = await self.db.execute(query)
        return [dict(row._mapping) for row in result.all()]

    async def confirmed_duplicate_counts(
        self, date_from: date, date_to: date, user_id: Optional[uuid.UUID] = None
    ) -> dict[uuid.UUID, int]:
        query = (
            select(Transaction.user_id, func.count(func.distinct(DuplicateFlag.id)))
            .join(Transaction, Transaction.transaction_id == DuplicateFlag.transaction_id)
            .where(
                DuplicateFlag.status == DuplicateFlagStatus.confirmed,
                Transaction.business_date >= date_from,
                Transaction.business_date <= date_to,
            )
            .group_by(Transaction.user_id)
        )
        if user_id:
            query = query.where(Transaction.user_id == user_id)

        result = await self.db.execute(query)
        return {row[0]: row[1] for row in result.all()}

    async def transfer_counts(
        self, date_from: date, date_to: date, user_id: Optional[uuid.UUID] = None
    ) -> dict[uuid.UUID, int]:
        query = (
            select(Transaction.user_id, func.count())
            .where(
                Transaction.was_transferred.is_(True),
                Transaction.business_date >= date_from,
                Transaction.business_date <= date_to,
            )
            .group_by(Transaction.user_id)
        )
        if user_id:
            query = query.where(Transaction.user_id == user_id)

        result = await self.db.execute(query)
        return {row[0]: row[1] for row in result.all()}
