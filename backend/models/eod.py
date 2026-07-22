import uuid
import enum
from datetime import datetime, timezone, date
from sqlalchemy import Date, DateTime, Enum as SAEnum, Text
from sqlalchemy.orm import Mapped, mapped_column
from ..core.database import CatalogBase


class EODStatus(str, enum.Enum):
    closed = "closed"
    reopened = "reopened"


class EODClosure(CatalogBase):
    __tablename__ = "eod_closures"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, unique=True, index=True, nullable=False)
    status: Mapped[EODStatus] = mapped_column(SAEnum(EODStatus), nullable=False, default=EODStatus.closed)

    # Plain UUIDs, not ForeignKeys: users live in the core database.
    closed_by_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    reopened_by_user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    reopened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    reopen_reason: Mapped[str] = mapped_column(Text, nullable=True)
