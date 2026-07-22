import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import DateTime, Enum as SAEnum, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from ..core.database import CatalogBase


class DuplicateFlagStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    dismissed = "dismissed"


class DuplicateFlag(CatalogBase):
    __tablename__ = "duplicate_flags"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    duplicate_of_transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.transaction_id"), nullable=False, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    matched_fields: Mapped[str] = mapped_column(Text, nullable=False)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id"), nullable=True, index=True
    )
    status: Mapped[DuplicateFlagStatus] = mapped_column(
        SAEnum(DuplicateFlagStatus), nullable=False, default=DuplicateFlagStatus.pending, index=True
    )
    # Plain UUID, not a ForeignKey: users live in the core database.
    reviewed_by_user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
