import uuid
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional
from sqlalchemy import String, DateTime, Date, Numeric, ForeignKey, Enum as SAEnum, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..core.database import CatalogBase
import enum


class BalanceStatus(str, enum.Enum):
    balanced = "BALANCED"
    not_balanced = "NOT BALANCED"
    pending = "PENDING"


class Transaction(CatalogBase):
    __tablename__ = "transactions"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        default=uuid.uuid4, primary_key=True, index=True
    )
    # Plain UUID, not a ForeignKey: users live in the core database, separate
    # from this catalog's database, so no cross-database FK is possible.
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), nullable=False, index=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.location_id"), nullable=False, index=True)
    bag_number: Mapped[str] = mapped_column(String(100), nullable=False)
    # Scanned in the wizard's third step. Nullable because transactions created
    # before that step existed have none — not a data-quality gap to backfill.
    wallet_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    total_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    balance_status: Mapped[BalanceStatus] = mapped_column(
        SAEnum(BalanceStatus), nullable=False, default=BalanceStatus.pending
    )
    expected_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    # The operating day this transaction counts toward. A wall-clock/business
    # concept (matches EODService's day-close checks and what an operator means
    # by "today"), so it uses local date rather than UTC. Independently mutable
    # from created_at (see TransactionService.transfer_business_date) so admins
    # can fix mis-dated entries without falsifying the real creation timestamp.
    business_date: Mapped[date] = mapped_column(
        Date, default=lambda: datetime.now().date(), index=True, nullable=False
    )
    # Denormalized for fast list-filtering without joining duplicate_flags.
    # Values: "none" | "pending_review" | "confirmed_duplicate" | "dismissed".
    duplicate_flag_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="none"
    )
    # Set by TransactionService.transfer_business_date whenever an admin moves
    # this row to a different business_date. Feeds the processor error-count
    # stat (StatsService) without needing to parse AuditLog text.
    was_transferred: Mapped[bool] = mapped_column(nullable=False, default=False)

    # Correction workflow (TransactionService.correct_transaction) is
    # append-only: a wrong transaction is never edited or deleted. Instead a
    # new row is created with original_transaction_id pointing back at it,
    # and the original is flagged is_superseded so both remain in the record
    # for audit purposes. correction_reason lives on the corrected row itself
    # (in addition to the audit log entry) so the detail page can show it
    # without a join.
    original_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("transactions.transaction_id"), nullable=True, index=True
    )
    is_superseded: Mapped[bool] = mapped_column(nullable=False, default=False)
    correction_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="transactions")
    location: Mapped["Location"] = relationship("Location", back_populates="transactions")
    denominations: Mapped[list["Denomination"]] = relationship(
        "Denomination", back_populates="transaction", cascade="all, delete-orphan"
    )


class Denomination(CatalogBase):
    __tablename__ = "denominations"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    denomination: Mapped[str] = mapped_column(String(20), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    transaction: Mapped["Transaction"] = relationship("Transaction", back_populates="denominations")


class AuditLog(CatalogBase):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    # Plain UUID, not a ForeignKey: users live in the core database.
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    details: Mapped[str] = mapped_column(Text, nullable=True)
