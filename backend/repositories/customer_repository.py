from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from ..models.customer import Customer, Location
from ..models.transaction import Transaction


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

    async def create(self, customer_id: str, customer_name: str) -> Customer:
        customer = Customer(customer_id=customer_id, customer_name=customer_name)
        self.db.add(customer)
        await self.db.flush()
        await self.db.refresh(customer)
        return customer

    async def update_name(self, customer_id: str, customer_name: str) -> Optional[Customer]:
        customer = await self.get_by_id(customer_id)
        if not customer:
            return None
        customer.customer_name = customer_name
        await self.db.flush()
        return customer

    async def rename(self, old_id: str, new_id: str) -> Optional[Customer]:
        customer = await self.get_by_id(old_id)
        if not customer:
            return None
        new_customer = Customer(customer_id=new_id, customer_name=customer.customer_name)
        self.db.add(new_customer)
        await self.db.flush()

        await self.db.execute(
            update(Location).where(Location.customer_id == old_id).values(customer_id=new_id)
        )
        await self.db.execute(
            update(Transaction).where(Transaction.customer_id == old_id).values(customer_id=new_id)
        )
        # customer.locations was eagerly loaded (via get_by_id's selectinload) before the
        # bulk update above repointed those rows to new_id. Without refreshing it here, the
        # ORM still sees them as old_id's children and the delete-orphan cascade below would
        # delete them for real, even though the DB no longer associates them with old_id.
        await self.db.refresh(customer, attribute_names=["locations"])
        await self.db.delete(customer)
        await self.db.flush()
        await self.db.refresh(new_customer)
        return new_customer

    async def delete(self, customer_id: str) -> bool:
        customer = await self.get_by_id(customer_id)
        if not customer:
            return False
        await self.db.delete(customer)
        await self.db.flush()
        return True

    async def has_dependents(self, customer_id: str) -> bool:
        result = await self.db.execute(
            select(Transaction.transaction_id).where(Transaction.customer_id == customer_id).limit(1)
        )
        return result.first() is not None

    async def create_location(self, customer_id: str, location_id: str, location_name: str) -> Location:
        location = Location(location_id=location_id, customer_id=customer_id, location_name=location_name)
        self.db.add(location)
        await self.db.flush()
        await self.db.refresh(location)
        return location

    async def update_location_name(self, location_id: str, location_name: str) -> Optional[Location]:
        location = await self.get_location(location_id)
        if not location:
            return None
        location.location_name = location_name
        await self.db.flush()
        return location

    async def rename_location(self, old_id: str, new_id: str) -> Optional[Location]:
        location = await self.get_location(old_id)
        if not location:
            return None
        new_location = Location(
            location_id=new_id, customer_id=location.customer_id, location_name=location.location_name
        )
        self.db.add(new_location)
        await self.db.flush()

        await self.db.execute(
            update(Transaction).where(Transaction.location_id == old_id).values(location_id=new_id)
        )
        await self.db.delete(location)
        await self.db.flush()
        await self.db.refresh(new_location)
        return new_location

    async def delete_location(self, location_id: str) -> bool:
        location = await self.get_location(location_id)
        if not location:
            return False
        await self.db.delete(location)
        await self.db.flush()
        return True

    async def location_has_transactions(self, location_id: str) -> bool:
        result = await self.db.execute(
            select(Transaction.transaction_id).where(Transaction.location_id == location_id).limit(1)
        )
        return result.first() is not None
