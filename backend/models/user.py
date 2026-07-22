import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from ..core.database import CoreBase
import enum


class UserRole(str, enum.Enum):
    cashier = "cashier"
    supervisor = "supervisor"
    administrator = "administrator"


class User(CoreBase):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        default=uuid.uuid4, primary_key=True, index=True
    )
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), nullable=False, default=UserRole.cashier)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    is_active: Mapped[bool] = mapped_column(default=True)
