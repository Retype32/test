import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from ..models.user import UserRole


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=4)
    role: UserRole = UserRole.cashier


class UserActiveUpdate(BaseModel):
    is_active: bool


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=100)
    password: Optional[str] = Field(None, min_length=4)
    role: Optional[UserRole] = None


class UserResponse(BaseModel):
    id: uuid.UUID
    username: str
    role: UserRole
    created_at: datetime
    is_active: bool

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
