import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Enum as SAEnum, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from ..core.database import CatalogBase


class NotificationEventType(str, enum.Enum):
    """Known event types. This is a documentation/validation aid, not a DB
    constraint — Notification.event_type is a plain string column so new
    event types never require a migration."""
    duplicate_transaction = "DUPLICATE_TRANSACTION"
    transaction_transferred = "TRANSACTION_TRANSFERRED"
    eod_reopened = "EOD_REOPENED"


class NotificationSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"


class NotificationStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"
    dismissed = "dismissed"


class Notification(CatalogBase):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    severity: Mapped[NotificationSeverity] = mapped_column(
        SAEnum(NotificationSeverity), nullable=False, default=NotificationSeverity.error
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=True)

    related_transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.transaction_id"), nullable=True, index=True
    )
    # Plain UUID, not a ForeignKey: users live in the core database.
    related_user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)

    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus), nullable=False, default=NotificationStatus.open, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    resolved_by_user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str] = mapped_column(Text, nullable=True)
