import uuid
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel
from ..models.eod import EODStatus


class EODCloseRequest(BaseModel):
    business_date: date


class EODReopenRequest(BaseModel):
    business_date: date
    reason: str


class EODClosureResponse(BaseModel):
    id: uuid.UUID
    business_date: date
    status: EODStatus
    # Optional: an automatic (scheduler-driven) close has no acting user.
    closed_by_user_id: Optional[uuid.UUID] = None
    closed_automatically: bool = False
    closed_at: datetime
    reopened_by_user_id: Optional[uuid.UUID] = None
    reopened_at: Optional[datetime] = None
    reopen_reason: Optional[str] = None

    model_config = {"from_attributes": True}
