from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from ..models.customer import Customer, Location


class CustomerRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, customer_id: str) -> Optional[Customer]:
        result = await self.db.execute(
            select(Customer)
            .options(selectinload(Customer.locations))
            .where(Customer.customer_id == customer_id)
        )
        return result.scalar_one_or_none()

    async def get_location(self, location_id: str) -> Optional[Location]:
        result = await self.db.execute(
            select(Location).where(Location.location_id == location_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Customer]:
        result = await self.db.execute(
            select(Customer).options(selectinload(Customer.locations)).order_by(Customer.customer_name)
        )
        return list(result.scalars().all())
