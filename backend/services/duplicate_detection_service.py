import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.transaction_repository import TransactionRepository
from ..repositories.duplicate_repository import DuplicateRepository
from ..services.notification_service import NotificationService
from ..models.transaction import Transaction
from ..models.notification import NotificationEventType, NotificationSeverity


def _denomination_signature(txn: Transaction) -> frozenset:
    return frozenset((d.denomination, d.count) for d in txn.denominations)


def _fields_match(a: Transaction, b: Transaction) -> list[str]:
    """Returns the list of fields that make a and b look like the same
    transaction entered twice. Empty list means they don't match."""
    checks = [
        ("customer_id", a.customer_id == b.customer_id),
        ("location_id", a.location_id == b.location_id),
        ("bag_number", a.bag_number.strip().lower() == b.bag_number.strip().lower()),
        ("total_value", a.total_value == b.total_value),
        ("denominations", _denomination_signature(a) == _denomination_signature(b)),
    ]
    if all(matched for _, matched in checks):
        return [name for name, _ in checks]
    return []


class DuplicateDetectionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.txn_repo = TransactionRepository(db)
        self.duplicate_repo = DuplicateRepository(db)
        self.notification_service = NotificationService(db)

    async def check_for_duplicate(self, new_txn: Transaction) -> Optional[Transaction]:
        candidates = await self.txn_repo.list_by_user_and_business_date(
            new_txn.user_id, new_txn.business_date, exclude_transaction_id=new_txn.transaction_id
        )

        for candidate in candidates:
            matched_fields = _fields_match(new_txn, candidate)
            if not matched_fields:
                continue

            flag = await self.duplicate_repo.create(
                transaction_id=new_txn.transaction_id,
                duplicate_of_transaction_id=candidate.transaction_id,
                matched_fields=matched_fields,
            )
            await self.txn_repo.set_duplicate_flag_status(new_txn.transaction_id, "pending_review")
            await self.txn_repo.set_duplicate_flag_status(candidate.transaction_id, "pending_review")

            notification = await self.notification_service.raise_event(
                event_type=NotificationEventType.duplicate_transaction.value,
                severity=NotificationSeverity.error.value,
                message=(
                    f"Possible duplicate: {new_txn.username} entered bag "
                    f"'{new_txn.bag_number}' twice for {new_txn.business_date}"
                ),
                related_transaction_id=new_txn.transaction_id,
                related_user_id=new_txn.user_id,
                details={
                    "duplicate_of_transaction_id": str(candidate.transaction_id),
                    "matched_fields": matched_fields,
                },
            )
            flag.notification_id = notification.id
            await self.db.flush()
            return candidate

        return None

    async def list_flags(self, status: Optional[str] = None, limit: int = 100):
        return await self.duplicate_repo.list_filtered(status=status, limit=limit)

    async def get_flag(self, flag_id: uuid.UUID):
        return await self.duplicate_repo.get_by_id(flag_id)

    async def review_flag(
        self, flag_id: uuid.UUID, status: str, reviewed_by_user_id: uuid.UUID, notes: Optional[str] = None
    ):
        flag = await self.duplicate_repo.get_by_id(flag_id)
        if not flag:
            raise ValueError(f"Duplicate flag {flag_id} not found")

        # S-09: segregation of duties -- a supervisor reviewing a duplicate
        # flag raised against their own transaction defeats the point of a
        # second set of eyes. Plain ValueError (not a distinct exception
        # type) deliberately: both callers of this service
        # (backend/api/routes/duplicates.py, web/routes/duplicates_web.py)
        # already catch ValueError for every other rejection this method
        # raises, so this guard needs no route change to take effect.
        flagged_txn = await self.txn_repo.get_by_id(flag.transaction_id)
        if flagged_txn and flagged_txn.user_id == reviewed_by_user_id:
            raise ValueError("A supervisor cannot review a duplicate flag on their own transaction")

        flag = await self.duplicate_repo.set_status(flag_id, status, reviewed_by_user_id)
        if not flag:
            raise ValueError(f"Duplicate flag {flag_id} not found")

        new_txn_status = "confirmed_duplicate" if status == "confirmed" else "dismissed"
        await self.txn_repo.set_duplicate_flag_status(flag.transaction_id, new_txn_status)
        await self.txn_repo.set_duplicate_flag_status(flag.duplicate_of_transaction_id, new_txn_status)

        if flag.notification_id:
            await self.notification_service.resolve(flag.notification_id, "resolved", reviewed_by_user_id, notes)

        return flag
