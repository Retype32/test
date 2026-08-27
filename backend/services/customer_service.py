import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from ..repositories.customer_repository import CustomerRepository
from ..repositories.transaction_repository import AuditRepository
from ..models.customer import Customer, Location


class CustomerService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CustomerRepository(db)
        self.audit_repo = AuditRepository(db)

    async def validate_customer(self, customer_id: str) -> Optional[Customer]:
        return await self.repo.get_by_id(customer_id)

    async def get_location(self, location_id: str) -> Optional[Location]:
        return await self.repo.get_location(location_id)

    async def list_customers(self) -> list[Customer]:
        return await self.repo.list_all()

    async def create_customer(self, customer_id: str, customer_name: str, actor_user_id: uuid.UUID) -> Customer:
        existing = await self.repo.get_by_id(customer_id)
        if existing:
            raise ValueError(f"Customer ID '{customer_id}' already exists")

        customer = await self.repo.create(customer_id, customer_name)
        await self.audit_repo.log(actor_user_id, "CUSTOMER_CREATED", f"Customer {customer_id} — {customer_name}")
        return customer

    async def update_customer(
        self, customer_id: str, new_customer_id: str, customer_name: str, actor_user_id: uuid.UUID
    ) -> Customer:
        customer = await self.repo.get_by_id(customer_id)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found")

        if new_customer_id != customer_id:
            existing = await self.repo.get_by_id(new_customer_id)
            if existing:
                raise ValueError(f"Customer ID '{new_customer_id}' already exists")
            customer = await self.repo.rename(customer_id, new_customer_id)
            await self.audit_repo.log(
                actor_user_id, "CUSTOMER_ID_CHANGED", f"Customer {customer_id} renamed to {new_customer_id}"
            )

        updated = await self.repo.update_name(customer.customer_id, customer_name)
        await self.audit_repo.log(
            actor_user_id, "CUSTOMER_UPDATED", f"Customer {updated.customer_id} — {customer_name}"
        )
        return updated

    async def delete_customer(self, customer_id: str, actor_user_id: uuid.UUID) -> None:
        customer = await self.repo.get_by_id(customer_id)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found")

        if await self.repo.has_dependents(customer_id):
            raise ValueError(f"Customer {customer_id} has existing transactions and cannot be deleted")

        await self.repo.delete(customer_id)
        await self.audit_repo.log(actor_user_id, "CUSTOMER_DELETED", f"Customer {customer_id}")

    async def create_location(
        self, customer_id: str, location_id: str, location_name: str, actor_user_id: uuid.UUID
    ) -> Location:
        customer = await self.repo.get_by_id(customer_id)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found")

        existing = await self.repo.get_location(location_id)
        if existing:
            raise ValueError(f"Location ID '{location_id}' already exists")

        location = await self.repo.create_location(customer_id, location_id, location_name)
        await self.audit_repo.log(
            actor_user_id, "LOCATION_CREATED", f"Location {location_id} — {location_name} (customer {customer_id})"
        )
        return location

    async def update_location(
        self,
        customer_id: str,
        location_id: str,
        new_location_id: str,
        location_name: str,
        actor_user_id: uuid.UUID,
    ) -> Location:
        location = await self.repo.get_location(location_id)
        if not location or location.customer_id != customer_id:
            raise ValueError(f"Location {location_id} not found")

        if new_location_id != location_id:
            existing = await self.repo.get_location(new_location_id)
            if existing:
                raise ValueError(f"Location ID '{new_location_id}' already exists")
            location = await self.repo.rename_location(location_id, new_location_id)
            await self.audit_repo.log(
                actor_user_id, "LOCATION_ID_CHANGED", f"Location {location_id} renamed to {new_location_id}"
            )

        updated = await self.repo.update_location_name(location.location_id, location_name)
        await self.audit_repo.log(
            actor_user_id, "LOCATION_UPDATED", f"Location {updated.location_id} — {location_name}"
        )
        return updated

    async def delete_location(self, customer_id: str, location_id: str, actor_user_id: uuid.UUID) -> None:
        location = await self.repo.get_location(location_id)
        if not location or location.customer_id != customer_id:
            raise ValueError(f"Location {location_id} not found")

        if await self.repo.location_has_transactions(location_id):
            raise ValueError(f"Location {location_id} has existing transactions and cannot be deleted")

        await self.repo.delete_location(location_id)
        await self.audit_repo.log(actor_user_id, "LOCATION_DELETED", f"Location {location_id}")
