import uuid
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel
from ..models.notification import NotificationSeverity, NotificationStatus


class NotificationResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    severity: NotificationSeverity
    message: str
    details_json: Optional[str] = None
    related_transaction_id: Optional[uuid.UUID] = None
    related_user_id: Optional[uuid.UUID] = None
    status: NotificationStatus
    created_at: datetime
    resolved_by_user_id: Optional[uuid.UUID] = None
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None

    model_config = {"from_attributes": True}


class NotificationResolveRequest(BaseModel):
    status: Literal["resolved", "dismissed"]
    notes: Optional[str] = None
