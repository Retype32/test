from typing import Optional
from sqlalchemy.exc import IntegrityError
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

    async def ensure_placeholder(self, identity_id: str, name: str) -> None:
        """Get-or-create a customer/location pair sharing one fixed id.

        Brink's Complete's bulk-bag flow never collects a real customer or
        location, but customer_id/location_id are real foreign keys on
        Transaction -- every Complete transaction files under this single
        placeholder identity instead. Safe to call every time a Complete
        session starts; only creates rows the first time this catalog is
        ever used.
        """
        if await self.repo.get_by_id(identity_id) is None:
            self.db.add(Customer(customer_id=identity_id, customer_name=name))
        if await self.repo.get_location(identity_id) is None:
            self.db.add(Location(location_id=identity_id, customer_id=identity_id, location_name=name))
        try:
            await self.db.commit()
        except IntegrityError:
            # Another request created the same rows concurrently -- fine,
            # the rows exist either way.
            await self.db.rollback()
