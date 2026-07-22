import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from ..core.database import CoreBase


class CoreAuditLog(CoreBase):
    __tablename__ = "core_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    details: Mapped[str] = mapped_column(Text, nullable=True)
