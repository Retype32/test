import re
import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from ..models.user import UserRole

# S-10: raised from a 4-character minimum with no complexity requirement
# (satisfied trivially by every seeded demo password) to a real minimum plus
# a character-class requirement. Not mode-gated -- a weak password is just
# as dangerous in development as in production.
PASSWORD_MIN_LENGTH = 10


def _validate_password_strength(password: str) -> str:
    classes = sum([
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"[0-9]", password)),
        bool(re.search(r"[^a-zA-Z0-9]", password)),
    ])
    if classes < 2:
        raise ValueError(
            "Password must contain characters from at least two of: lowercase "
            "letters, uppercase letters, digits, symbols."
        )
    return password


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH)
    role: UserRole = UserRole.cashier

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class UserActiveUpdate(BaseModel):
    is_active: bool


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=100)
    password: Optional[str] = Field(None, min_length=PASSWORD_MIN_LENGTH)
    role: Optional[UserRole] = None

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_password_strength(v)


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
