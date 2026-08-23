import uuid
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, case
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError
from ..models.transaction import Transaction, Denomination, AuditLog, BalanceStatus
from ..models.idempotency import IdempotencyKey


# Default idempotency-key lifetime -- 01_transaction_integrity.md §7
# recommends 24-48h as enough to cover realistic client retry windows
# without growing the table unboundedly.
IDEMPOTENCY_KEY_TTL_HOURS = 48


async def _conflict(db: AsyncSession, message: str) -> ValueError:
    """Every StaleDataError/IntegrityError catch below needs the same
    recovery step before it's safe to raise: the failed flush leaves the
    session's transaction in a "pending rollback" state (confirmed
    empirically -- a plain SELECT after an uncaught-and-unrolled-back
    StaleDataError/IntegrityError raises PendingRollbackError), so any
    caller that goes on to catch the resulting ValueError and keep using
    the same request-scoped session (e.g. a web route re-rendering a form
    after a conflict) would otherwise blow up on its very next query.
    Rolling back here is safe precisely because nothing in this session has
    committed yet (the request's single commit point is still ahead, in
    get_catalog_db) -- discarding this attempt's uncommitted work and
    nothing else."""
    await db.rollback()
    return ValueError(message)


class TransactionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        user_id: uuid.UUID,
        username: str,
        customer_id: str,
        location_id: str,
        bag_number: str,
        total_value: Decimal,
        expected_total: Optional[Decimal],
        balance_status: BalanceStatus,
        denominations: list[dict],
        wallet_id: Optional[str] = None,
        business_date: Optional[date] = None,
        original_transaction_id: Optional[uuid.UUID] = None,
        correction_reason: Optional[str] = None,
    ) -> Transaction:
        txn = Transaction(
            user_id=user_id,
            username=username,
            customer_id=customer_id,
            location_id=location_id,
            bag_number=bag_number,
            wallet_id=wallet_id,
            total_value=total_value,
            expected_total=expected_total,
            balance_status=balance_status,
            original_transaction_id=original_transaction_id,
            correction_reason=correction_reason,
        )
        if business_date is not None:
            txn.business_date = business_date
        self.db.add(txn)
        await self.db.flush()

        for d in denominations:
            denom = Denomination(
                transaction_id=txn.transaction_id,
                denomination=d["denomination"],
                count=d["count"],
                value=d["value"],
            )
            self.db.add(denom)

        await self.db.flush()
        await self.db.refresh(txn)
        return txn

    async def update_business_date(
        self, transaction_id: uuid.UUID, new_business_date: date
    ) -> Optional[Transaction]:
        txn = await self.get_by_id(transaction_id)
        if not txn:
            return None
        txn.business_date = new_business_date
        txn.was_transferred = True
        try:
            await self.db.flush()
        except StaleDataError:
            # H-2: this row was modified by another request between our read
            # and this write (optimistic-locking version mismatch) -- a
            # clean conflict, not a silent lost update.
            raise await _conflict(
                self.db, f"Transaction {transaction_id} was modified by another request; please retry"
            )
        await self.db.refresh(txn)
        return txn

    async def list_by_business_date(self, business_date: date) -> list[Transaction]:
        result = await self.db.execute(
            select(Transaction).where(Transaction.business_date == business_date)
        )
        return list(result.scalars().all())

    async def list_by_user_and_business_date(
        self, user_id: uuid.UUID, business_date: date, exclude_transaction_id: Optional[uuid.UUID] = None
    ) -> list[Transaction]:
        query = (
            select(Transaction)
            .options(selectinload(Transaction.denominations))
            .where(Transaction.user_id == user_id, Transaction.business_date == business_date)
        )
        if exclude_transaction_id:
            query = query.where(Transaction.transaction_id != exclude_transaction_id)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def set_duplicate_flag_status(self, transaction_id: uuid.UUID, status: str):
        txn = await self.get_by_id(transaction_id)
        if txn:
            txn.duplicate_flag_status = status
            try:
                await self.db.flush()
            except StaleDataError:
                raise await _conflict(
                    self.db, f"Transaction {transaction_id} was modified by another request; please retry"
                )

    async def set_superseded(self, transaction_id: uuid.UUID):
        txn = await self.get_by_id(transaction_id)
        if txn:
            txn.is_superseded = True
            try:
                await self.db.flush()
            except StaleDataError:
                raise await _conflict(
                    self.db, f"Transaction {transaction_id} was modified by another request; please retry"
                )

    async def list_corrections(self, original_transaction_id: uuid.UUID) -> list[Transaction]:
        result = await self.db.execute(
            select(Transaction)
            .where(Transaction.original_transaction_id == original_transaction_id)
            .order_by(Transaction.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_id(self, transaction_id: uuid.UUID) -> Optional[Transaction]:
        result = await self.db.execute(
            select(Transaction)
            .options(
                selectinload(Transaction.denominations),
                selectinload(Transaction.customer),
                selectinload(Transaction.location),
            )
            .where(Transaction.transaction_id == transaction_id)
        )
        return result.scalar_one_or_none()

    async def list_filtered(
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
        query = (
            select(Transaction)
            .options(
                selectinload(Transaction.customer),
                selectinload(Transaction.location),
                selectinload(Transaction.denominations),
            )
            # NOT BALANCED transactions sort first so they're visible the
            # moment the viewer is opened, with no filter or extra click
            # needed to find them.
            .order_by(
                case((Transaction.balance_status == BalanceStatus.not_balanced, 0), else_=1),
                Transaction.created_at.desc(),
            )
        )

        conditions = []
        if date_from:
            conditions.append(Transaction.created_at >= date_from)
        if date_to:
            conditions.append(Transaction.created_at <= date_to)
        if customer_id:
            conditions.append(Transaction.customer_id == customer_id)
        if location_id:
            conditions.append(Transaction.location_id == location_id)
        if user_id:
            conditions.append(Transaction.user_id == user_id)
        if business_date:
            conditions.append(Transaction.business_date == business_date)

        if conditions:
            query = query.where(and_(*conditions))

        query = query.limit(limit).offset(offset)
        result = await self.db.execute(query)
        return list(result.scalars().all())


class AuditRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(self, user_id: Optional[uuid.UUID], action: str, details: Optional[str] = None):
        entry = AuditLog(user_id=user_id, action=action, details=details)
        self.db.add(entry)
        await self.db.flush()

    async def list_recent(self, limit: int = 100) -> list[AuditLog]:
        result = await self.db.execute(
            select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
        )
        return list(result.scalars().all())


class IdempotencyRepository:
    """Backs the idempotency contract in
    docs/production_readiness/01_transaction_integrity.md §6.1/§7 --
    check-and-insert against idempotency_keys in the same request-scoped
    session as the business write it protects. `key` here is always the
    full scope-qualified composite string (see
    TransactionService._idempotency_composite_key), never a bare
    client-supplied value, so a lookup can never cross endpoints."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, composite_key: str) -> Optional[IdempotencyKey]:
        result = await self.db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == composite_key)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        composite_key: str,
        catalog: str,
        scope: str,
        transaction_id: uuid.UUID,
        request_fingerprint: Optional[str] = None,
    ) -> IdempotencyKey:
        now = datetime.now(timezone.utc)
        row = IdempotencyKey(
            key=composite_key,
            catalog=catalog,
            scope=scope,
            request_fingerprint=request_fingerprint,
            transaction_id=transaction_id,
            created_at=now,
            expires_at=now + timedelta(hours=IDEMPOTENCY_KEY_TTL_HOURS),
        )
        self.db.add(row)
        # A concurrent winner's row (same composite key) raises IntegrityError
        # here -- callers (TransactionService) catch it, roll back, and
        # re-fetch the winner's transaction_id via get() above.
        await self.db.flush()
        return row
