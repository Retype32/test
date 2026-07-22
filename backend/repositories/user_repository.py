import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.user import User, UserRole


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> Optional[User]:
        result = await self.db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    async def create(self, username: str, password_hash: str, role: UserRole) -> User:
        user = User(username=username, password_hash=password_hash, role=role)
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def list_all(self) -> list[User]:
        result = await self.db.execute(select(User).order_by(User.username))
        return list(result.scalars().all())

    async def update_active(self, user_id: uuid.UUID, is_active: bool) -> Optional[User]:
        user = await self.get_by_id(user_id)
        if user:
            user.is_active = is_active
            await self.db.flush()
        return user
