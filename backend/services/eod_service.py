import uuid
from datetime import date
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.eod_repository import EODRepository
from ..repositories.transaction_repository import AuditRepository
from ..services.notification_service import NotificationService
from ..models.eod import EODClosure, EODStatus
from ..models.notification import NotificationEventType, NotificationSeverity


class EODService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = EODRepository(db)
        self.audit_repo = AuditRepository(db)
        self.notification_service = NotificationService(db)

    async def is_day_closed(self, business_date: date) -> bool:
        closure = await self.repo.get_by_date(business_date)
        return closure is not None and closure.status == EODStatus.closed

    async def close_day(
        self, business_date: date, closed_by_user_id: Optional[uuid.UUID], automatic: bool = False
    ) -> EODClosure:
        if await self.is_day_closed(business_date):
            raise ValueError(f"Business day {business_date} is already closed")

        closure = await self.repo.close(business_date, closed_by_user_id, automatic=automatic)
        detail = f"business_date={business_date}" + (" (automatic)" if automatic else "")
        await self.audit_repo.log(closed_by_user_id, "EOD_CLOSED", detail)
        return closure

    async def reopen_day(
        self, business_date: date, reopened_by_user_id: uuid.UUID, reason: str
    ) -> EODClosure:
        closure = await self.repo.reopen(business_date, reopened_by_user_id, reason)
        if not closure:
            raise ValueError(f"Business day {business_date} is not currently closed")

        await self.audit_repo.log(
            reopened_by_user_id, "EOD_REOPENED", f"business_date={business_date}, reason={reason}"
        )
        await self.notification_service.raise_event(
            event_type=NotificationEventType.eod_reopened.value,
            severity=NotificationSeverity.warning.value,
            message=f"Business day {business_date} was reopened by an administrator",
            related_user_id=reopened_by_user_id,
            details={"business_date": str(business_date), "reason": reason},
        )
        return closure

    async def list_closures(
        self, date_from: Optional[date] = None, date_to: Optional[date] = None
    ) -> list[EODClosure]:
        return await self.repo.list_between(date_from, date_to)
