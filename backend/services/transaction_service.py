import uuid
from decimal import Decimal
from datetime import datetime, date
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from ..repositories.transaction_repository import TransactionRepository, AuditRepository, IdempotencyRepository
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
        self.idempotency_repo = IdempotencyRepository(db)

    @staticmethod
    def _idempotency_composite_key(scope: str, client_key: Optional[str]) -> Optional[str]:
        # Scope the key to (catalog, endpoint, key) per
        # 01_transaction_integrity.md §7: the catalog is already implicit
        # (idempotency_keys lives per-catalog DB), and folding `scope` into
        # the stored primary key itself means a nonce minted for one
        # endpoint (say, wizard_complete) can never collide with the same
        # raw value reused against a different endpoint (correct_transaction).
        if not client_key:
            return None
        return f"{scope}:{client_key}"

    async def _existing_idempotent_result(self, composite_key: Optional[str]) -> Optional[Transaction]:
        if not composite_key:
            return None
        existing = await self.idempotency_repo.get(composite_key)
        if existing and existing.transaction_id:
            return await self.repo.get_by_id(existing.transaction_id)
        return None

    @staticmethod
    def _mark_replay(txn: Transaction) -> Transaction:
        # A duplicate key within scope returns the previously-created
        # transaction, not a new insert (§7) -- routes use this plain
        # (unmapped, so harmless on the ORM object) attribute to answer 200
        # instead of 201 for a replay, without changing this method's
        # return type for the many callers that only want the Transaction.
        txn._idempotent_replay = True
        return txn

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

    async def create_transaction(
        self,
        user_id: uuid.UUID,
        username: str,
        data: TransactionCreate,
        idempotency_key: Optional[str] = None,
        catalog: str = "",
    ) -> Transaction:
        # C-1 / consolidated plan §7: a client-supplied Idempotency-Key (REST
        # header) or wizard nonce (web form) collapses a retried/double-
        # submitted create into the original result instead of a second,
        # fully-accepted financial row. None (the default) preserves today's
        # behavior exactly -- every existing caller that doesn't pass one
        # keeps working unchanged.
        scope = "create_transaction"
        composite_key = self._idempotency_composite_key(scope, idempotency_key)
        existing = await self._existing_idempotent_result(composite_key)
        if existing:
            return self._mark_replay(existing)

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

        if composite_key:
            try:
                await self.idempotency_repo.create(composite_key, catalog, scope, txn.transaction_id)
            except IntegrityError:
                # A genuinely concurrent request with the same key won the
                # race and already committed its own transaction+key row
                # (this session's insert only fails once that's true, since
                # the unique-index conflict isn't visible until the other
                # side commits) -- discard this attempt entirely and return
                # the winner's result instead, exactly as a sequential
                # retry would see it.
                await self.db.rollback()
                winner = await self._existing_idempotent_result(composite_key)
                if winner:
                    return self._mark_replay(winner)
                raise
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
    ) -> list[Transaction]:
        return await self.repo.list_filtered(
            date_from=date_from,
            date_to=date_to,
            customer_id=customer_id,
            location_id=location_id,
            user_id=user_id,
            business_date=business_date,
            limit=limit,
            offset=offset,
        )

    async def _assert_transfer_allowed(self, txn: Transaction, new_business_date: date) -> None:
        # H-4: BalanceStatus/EOD-closed status never gated mutation before
        # this fix -- a transaction could be transferred OUT of an already-
        # closed day (silently changing what was supposed to be a fixed,
        # closed snapshot) with no reopen required. Both sides are checked
        # on every call, never just the target.
        if await self.eod_service.is_day_closed(txn.business_date):
            raise ValueError(
                f"Cannot transfer transaction {txn.transaction_id}: its current business day "
                f"{txn.business_date} is closed — reopen that day first"
            )
        if await self.eod_service.is_day_closed(new_business_date):
            raise ValueError(f"Cannot transfer into {new_business_date}: that business day is closed")

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

        await self._assert_transfer_allowed(txn, new_business_date)

        return await self._transfer_one(txn, new_business_date, reason, admin_user_id)

    async def bulk_transfer_business_date(
        self,
        transaction_ids: list[uuid.UUID],
        new_business_date: date,
        reason: str,
        admin_user_id: uuid.UUID,
    ) -> dict:
        moved: list[Transaction] = []
        missing: list[uuid.UUID] = []
        for transaction_id in transaction_ids:
            txn = await self.repo.get_by_id(transaction_id)
            if not txn:
                missing.append(transaction_id)
                continue
            # H-4: re-checked per row, not once before the loop starts -- a
            # day (either side) can close mid-loop, via a concurrent manual
            # close or the nightly scheduler firing mid-request for a large
            # batch.
            await self._assert_transfer_allowed(txn, new_business_date)
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
        idempotency_key: Optional[str] = None,
        catalog: str = "",
    ) -> Transaction:
        # C-1/M-2: same idempotency contract as create_transaction (§7),
        # now available on both the correction web form and the REST
        # endpoint. None (the default) is fully backward compatible.
        scope = "correct_transaction"
        composite_key = self._idempotency_composite_key(scope, idempotency_key)
        existing = await self._existing_idempotent_result(composite_key)
        if existing:
            return self._mark_replay(existing)

        original = await self.repo.get_by_id(original_transaction_id)
        if not original:
            raise ValueError(f"Transaction {original_transaction_id} not found")
        if original.is_superseded:
            raise ValueError("This transaction has already been corrected")
        if original.original_transaction_id is not None:
            raise ValueError("Cannot correct a correction — correct the original transaction instead")
        # S-09: segregation of duties -- a supervisor reviewing/correcting
        # their own work defeats the point of a second set of eyes.
        if supervisor_user_id == original.user_id:
            raise PermissionError("A supervisor cannot correct their own transaction")

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
        try:
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
        except IntegrityError:
            # H-3: the unique partial index on original_transaction_id
            # caught a double-correction race the TOCTOU checks above
            # couldn't -- a concurrent request corrected the same original
            # first and already committed. Clean conflict, not a raw 500.
            await self.db.rollback()
            raise ValueError(
                f"Transaction {original_transaction_id} was already corrected by another request"
            ) from None

        await self.repo.set_superseded(original.transaction_id)

        await self.audit_repo.log(
            supervisor_user_id,
            "TRANSACTION_CORRECTED",
            f"Transaction {original.transaction_id} corrected by {corrected.transaction_id}. Reason: {reason}",
        )

        corrected = await self.repo.get_by_id(corrected.transaction_id)

        if composite_key:
            try:
                await self.idempotency_repo.create(composite_key, catalog, scope, corrected.transaction_id)
            except IntegrityError:
                await self.db.rollback()
                winner = await self._existing_idempotent_result(composite_key)
                if winner:
                    return self._mark_replay(winner)
                raise
        return corrected
