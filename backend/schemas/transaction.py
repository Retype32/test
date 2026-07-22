import uuid
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field
from ..models.transaction import BalanceStatus


class DenominationEntry(BaseModel):
    denomination: str
    count: int = Field(..., ge=0)
    value: Decimal


class DenominationResponse(BaseModel):
    id: uuid.UUID
    denomination: str
    count: int
    value: Decimal

    model_config = {"from_attributes": True}


class TransactionCreate(BaseModel):
    customer_id: str
    location_id: str
    bag_number: str = Field(..., min_length=1, max_length=100)
    total_value: Decimal
    expected_total: Optional[Decimal] = None
    denominations: list[DenominationEntry]


class TransactionResponse(BaseModel):
    transaction_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    customer_id: str
    location_id: str
    bag_number: str
    total_value: Decimal
    expected_total: Optional[Decimal] = None
    balance_status: BalanceStatus
    created_at: datetime
    business_date: date
    duplicate_flag_status: str
    denominations: list[DenominationResponse] = []

    model_config = {"from_attributes": True}


class TransactionListItem(BaseModel):
    transaction_id: uuid.UUID
    created_at: datetime
    business_date: date
    duplicate_flag_status: str
    username: str
    customer_name: str
    location_name: str
    bag_number: str
    total_value: Decimal
    balance_status: BalanceStatus

    model_config = {"from_attributes": True}


class TransactionFilter(BaseModel):
    date_from: datetime | None = None
    date_to: datetime | None = None
    customer_id: str | None = None
    location_id: str | None = None
    user_id: uuid.UUID | None = None
    business_date: date | None = None


class TransactionTransferRequest(BaseModel):
    new_business_date: date
    reason: str = Field(..., min_length=1)
