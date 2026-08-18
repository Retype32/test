import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.user_repository import UserRepository
from ..repositories.core_audit_repository import CoreAuditRepository
from ..core.security import hash_password, verify_password, create_access_token
from ..models.user import User, UserRole
from ..schemas.user import UserCreate, UserUpdate, TokenResponse, UserResponse


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.user_repo = UserRepository(db)
        self.audit_repo = CoreAuditRepository(db)

    async def login(self, username: str, password: str) -> Optional[TokenResponse]:
        user = await self.user_repo.get_by_username(username)
        if not user or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None

        token = create_access_token(str(user.id))
        await self.audit_repo.log(user.id, "USER_LOGIN", f"User {username} logged in")
        return TokenResponse(
            access_token=token,
            user=UserResponse.model_validate(user),
        )

    async def create_user(self, data: UserCreate) -> User:
        existing = await self.user_repo.get_by_username(data.username)
        if existing:
            raise ValueError(f"Username '{data.username}' already exists")
        hashed = hash_password(data.password)
        return await self.user_repo.create(data.username, hashed, data.role)

    async def get_user(self, user_id: uuid.UUID) -> Optional[User]:
        return await self.user_repo.get_by_id(user_id)

    async def list_users(self) -> list[User]:
        return await self.user_repo.list_all()

    async def set_active(self, user_id: uuid.UUID, is_active: bool, actor_user_id: uuid.UUID) -> Optional[User]:
        if not is_active and user_id == actor_user_id:
            raise ValueError("You cannot deactivate your own account")

        user = await self.user_repo.update_active(user_id, is_active)
        if not user:
            raise ValueError(f"User {user_id} not found")
        action = "USER_ACTIVATED" if is_active else "USER_DEACTIVATED"
        await self.audit_repo.log(actor_user_id, action, f"Target user: {user.username}")
        return user

    async def update_user(self, user_id: uuid.UUID, data: UserUpdate, actor_user_id: uuid.UUID) -> User:
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        if data.role is not None and data.role != UserRole.administrator and user_id == actor_user_id:
            raise ValueError("You cannot change your own role away from administrator")

        if data.username is not None and data.username != user.username:
            existing = await self.user_repo.get_by_username(data.username)
            if existing:
                raise ValueError(f"Username '{data.username}' already exists")

        updated = await self.user_repo.update(user_id, username=data.username, role=data.role)
        if data.password:
            updated = await self.user_repo.update_password(user_id, hash_password(data.password))

        await self.audit_repo.log(actor_user_id, "USER_UPDATED", f"Target user: {updated.username}")
        return updated

    async def delete_user(self, user_id: uuid.UUID, actor_user_id: uuid.UUID) -> None:
        if user_id == actor_user_id:
            raise ValueError("You cannot delete your own account")

        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        username = user.username
        await self.user_repo.delete(user_id)
        await self.audit_repo.log(actor_user_id, "USER_DELETED", f"Target user: {username}")
