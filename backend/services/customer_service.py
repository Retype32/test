from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.customer_repository import CustomerRepository
from ..models.customer import Customer, Location


class CustomerService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CustomerRepository(db)

    async def validate_customer(self, customer_id: str) -> Optional[Customer]:
        return await self.repo.get_by_id(customer_id)

    async def get_location(self, location_id: str) -> Optional[Location]:
        return await self.repo.get_location(location_id)

    async def list_customers(self) -> list[Customer]:
        return await self.repo.list_all()
