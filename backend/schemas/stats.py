import uuid
from datetime import date
from decimal import Decimal
from pydantic import BaseModel


class ProcessorStatResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    date_from: date
    date_to: date
    slip_count: int
    slips_per_hour: float
    target_per_hour: int
    cash_volume: Decimal
    duplicate_error_count: int
    transfer_error_count: int
    error_count: int
