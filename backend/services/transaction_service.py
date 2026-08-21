import uuid
from decimal import Decimal
from datetime import datetime, date
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.transaction_repository import TransactionRepository, AuditRepository
from ..repositories.customer_repository import CustomerRepository
from ..services.eod_service import EODService
from ..services.notification_service import NotificationService
from ..services.duplicate_detection_service import DuplicateDetectionService
from ..models.transaction import Transaction, BalanceStatus
from ..models.notification import NotificationEventType, NotificationSeverity
from ..schemas.transaction import TransactionCreate


DENOMINATION_VALUES = {
    "€500": Decimal("500"),
    "€200": Decimal("200"),
    "€100": Decimal("100"),
    "€50": Decimal("50"),
    "€20": Decimal("20"),
    "€10": Decimal("10"),
    "€5": Decimal("5"),
    "Coins": Decimal("1"),
}


class TransactionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = TransactionRepository(db)
        self.customer_repo = CustomerRepository(db)
        self.audit_repo = AuditRepository(db)
        self.eod_service = EODService(db)
        self.notification_service = NotificationService(db)
        self.duplicate_service = DuplicateDetectionService(db)

    def calculate_balance_status(
        self, total_value: Decimal, expected_total: Optional[Decimal]
    ) -> BalanceStatus:
        # "Balanced" means the cash actually counted matches what was expected
        # for this bag. With nothing declared to check against, there's no
        # discrepancy to report either way, so the status stays PENDING
        # rather than defaulting to a (misleading) BALANCED.
        if expected_total is None:
            return BalanceStatus.pending
        return BalanceStatus.balanced if total_value == expected_total else BalanceStatus.not_balanced

    async def create_transaction(self, user_id: uuid.UUID, username: str, data: TransactionCreate) -> Transaction:
        today = datetime.now().date()
        if await self.eod_service.is_day_closed(today):
            raise ValueError(f"Business day {today} is closed for this catalog")

        customer = await self.customer_repo.get_by_id(data.customer_id)
        if not customer:
            raise ValueError(f"Customer {data.customer_id} not found")

        location = await self.customer_repo.get_location(data.location_id)
        if not location:
            raise ValueError(f"Location {data.location_id} not found")
        if location.customer_id != data.customer_id:
            raise ValueError("Location does not belong to this customer")

        denom_list = [
            {"denomination": d.denomination, "count": d.count, "value": d.value}
            for d in data.denominations
        ]
        balance_status = self.calculate_balance_status(data.total_value, data.expected_total)

        txn = await self.repo.create(
            user_id=user_id,
            username=username,
            customer_id=data.customer_id,
            location_id=data.location_id,
            bag_number=data.bag_number,
            wallet_id=data.wallet_id,
            total_value=data.total_value,
            expected_total=data.expected_total,
            balance_status=balance_status,
            denominations=denom_list,
        )

        await self.audit_repo.log(
            user_id, "TRANSACTION_CREATED",
            f"Transaction {txn.transaction_id} created for customer {data.customer_id}, bag {data.bag_number}"
        )

        # Reload with denominations eagerly loaded (required for the duplicate
        # comparison in an async session — the collection isn't populated by
        # the plain inserts above) and to check for duplicates.
        txn = await self.repo.get_by_id(txn.transaction_id)
        await self.duplicate_service.check_for_duplicate(txn)
        return txn

    async def get_transaction(self, transaction_id: uuid.UUID) -> Optional[Transaction]:
        return await self.repo.get_by_id(transaction_id)

    async def list_corrections(self, original_transaction_id: uuid.UUID) -> list[Transaction]:
        return await self.repo.list_corrections(original_transaction_id)

    async def list_transactions(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        customer_id: Optional[str] = None,
        location_id: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
        business_date: Optional[date] = None,
        limit: int = 500,
        offset: int = 0,
        exclude_superseded: bool = False,
    ) -> list[Transaction]:
        return await self.repo.list_filtered(
            date_from=date_from,
            date_to=date_to,
            customer_id=customer_id,
            location_id=location_id,
            user_id=user_id,
            exclude_superseded=exclude_superseded,
            business_date=business_date,
            limit=limit,
            offset=offset,
        )

    async def transfer_business_date(
        self,
        transaction_id: uuid.UUID,
        new_business_date: date,
        reason: str,
        admin_user_id: uuid.UUID,
    ) -> Transaction:
        txn = await self.repo.get_by_id(transaction_id)
        if not txn:
            raise ValueError(f"Transaction {transaction_id} not found")

        if await self.eod_service.is_day_closed(new_business_date):
            raise ValueError(f"Cannot transfer into {new_business_date}: that business day is closed")

        return await self._transfer_one(txn, new_business_date, reason, admin_user_id)

    async def bulk_transfer_business_date(
        self,
        transaction_ids: list[uuid.UUID],
        new_business_date: date,
        reason: str,
        admin_user_id: uuid.UUID,
    ) -> dict:
        if await self.eod_service.is_day_closed(new_business_date):
            raise ValueError(f"Cannot transfer into {new_business_date}: that business day is closed")

        moved: list[Transaction] = []
        missing: list[uuid.UUID] = []
        for transaction_id in transaction_ids:
            txn = await self.repo.get_by_id(transaction_id)
            if not txn:
                missing.append(transaction_id)
                continue
            moved.append(await self._transfer_one(txn, new_business_date, reason, admin_user_id))
        return {"moved": moved, "missing": missing}

    async def transfer_business_date_for_cashier(
        self,
        cashier_user_id: uuid.UUID,
        business_date: date,
        new_business_date: date,
        reason: str,
        admin_user_id: uuid.UUID,
    ) -> dict:
        txns = await self.repo.list_by_user_and_business_date(cashier_user_id, business_date)
        if not txns:
            raise ValueError("That cashier has no transactions on that business date")
        transaction_ids = [t.transaction_id for t in txns]
        return await self.bulk_transfer_business_date(transaction_ids, new_business_date, reason, admin_user_id)

    async def _transfer_one(
        self, txn: Transaction, new_business_date: date, reason: str, admin_user_id: uuid.UUID
    ) -> Transaction:
        old_business_date = txn.business_date
        updated = await self.repo.update_business_date(txn.transaction_id, new_business_date)

        await self.audit_repo.log(
            admin_user_id,
            "TRANSACTION_DAY_TRANSFERRED",
            f"Transaction {txn.transaction_id} moved from {old_business_date} to {new_business_date}. Reason: {reason}",
        )
        await self.notification_service.raise_event(
            event_type=NotificationEventType.transaction_transferred.value,
            severity=NotificationSeverity.warning.value,
            message=(
                f"Transaction for bag '{updated.bag_number}' moved from {old_business_date} "
                f"to {new_business_date} by an administrator"
            ),
            related_transaction_id=txn.transaction_id,
            related_user_id=admin_user_id,
            details={"from": str(old_business_date), "to": str(new_business_date), "reason": reason},
        )
        return updated

    async def correct_transaction(
        self,
        original_transaction_id: uuid.UUID,
        data: TransactionCreate,
        reason: str,
        supervisor_user_id: uuid.UUID,
    ) -> Transaction:
        original = await self.repo.get_by_id(original_transaction_id)
        if not original:
            raise ValueError(f"Transaction {original_transaction_id} not found")
        if original.is_superseded:
            raise ValueError("This transaction has already been corrected")
        if original.original_transaction_id is not None:
            raise ValueError("Cannot correct a correction — correct the original transaction instead")

        customer = await self.customer_repo.get_by_id(data.customer_id)
        if not customer:
            raise ValueError(f"Customer {data.customer_id} not found")

        location = await self.customer_repo.get_location(data.location_id)
        if not location:
            raise ValueError(f"Location {data.location_id} not found")
        if location.customer_id != data.customer_id:
            raise ValueError("Location does not belong to this customer")

        denom_list = [
            {"denomination": d.denomination, "count": d.count, "value": d.value}
            for d in data.denominations
        ]
        balance_status = self.calculate_balance_status(data.total_value, data.expected_total)

        # Corrections are append-only and never touch a closed business day's
        # transaction-creation gate (EODService.is_day_closed): they don't add
        # new activity to that day, they fix the record for a day that may
        # well already be closed — that's the whole point of the workflow.
        corrected = await self.repo.create(
            user_id=original.user_id,
            username=original.username,
            customer_id=data.customer_id,
            location_id=data.location_id,
            bag_number=data.bag_number,
            wallet_id=data.wallet_id,
            total_value=data.total_value,
            expected_total=data.expected_total,
            balance_status=balance_status,
            denominations=denom_list,
            business_date=original.business_date,
            original_transaction_id=original.transaction_id,
            correction_reason=reason,
        )

        await self.repo.set_superseded(original.transaction_id)

        await self.audit_repo.log(
            supervisor_user_id,
            "TRANSACTION_CORRECTED",
            f"Transaction {original.transaction_id} corrected by {corrected.transaction_id}. Reason: {reason}",
        )

        corrected = await self.repo.get_by_id(corrected.transaction_id)
        return corrected
