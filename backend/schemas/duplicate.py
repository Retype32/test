import uuid
import json
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, field_validator
from ..models.duplicate import DuplicateFlagStatus


class DuplicateFlagResponse(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    duplicate_of_transaction_id: uuid.UUID
    matched_fields: list[str]
    status: DuplicateFlagStatus
    detected_at: datetime
    reviewed_by_user_id: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @field_validator("matched_fields", mode="before")
    @classmethod
    def _parse_matched_fields(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


class DuplicateReviewRequest(BaseModel):
    status: Literal["confirmed", "dismissed"]
    notes: Optional[str] = None
