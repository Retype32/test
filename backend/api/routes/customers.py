from fastapi import APIRouter, HTTPException
from ...services.customer_service import CustomerService
from ...schemas.customer import (
    CustomerResponse,
    CustomerWithLocations,
    LocationResponse,
    CustomerCreate,
    CustomerUpdate,
    LocationCreate,
    LocationUpdate,
)
from ..deps import CurrentUser, CatalogDB, AdminOnly

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/{customer_id}", response_model=CustomerWithLocations)
async def validate_customer(
    customer_id: str,
    db: CatalogDB,
    _: CurrentUser,
):
    service = CustomerService(db)
    customer = await service.validate_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")
    return CustomerWithLocations.model_validate(customer)


@router.get("/{customer_id}/locations/{location_id}", response_model=LocationResponse)
async def get_location(
    customer_id: str,
    location_id: str,
    db: CatalogDB,
    _: CurrentUser,
):
    service = CustomerService(db)
    location = await service.get_location(location_id)
    if not location or location.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Location not found")
    return LocationResponse.model_validate(location)


@router.get("/", response_model=list[CustomerWithLocations])
async def list_customers(
    db: CatalogDB,
    _: CurrentUser,
):
    service = CustomerService(db)
    customers = await service.list_customers()
    return [CustomerWithLocations.model_validate(c) for c in customers]


@router.post("/", response_model=CustomerResponse, status_code=201)
async def create_customer(
    data: CustomerCreate,
    db: CatalogDB,
    admin: AdminOnly,
):
    service = CustomerService(db)
    try:
        customer = await service.create_customer(data.customer_id, data.customer_name, admin.id)
        return CustomerResponse.model_validate(customer)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: str,
    data: CustomerUpdate,
    db: CatalogDB,
    admin: AdminOnly,
):
    service = CustomerService(db)
    try:
        customer = await service.update_customer(customer_id, data.customer_id, data.customer_name, admin.id)
        return CustomerResponse.model_validate(customer)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{customer_id}", status_code=204)
async def delete_customer(
    customer_id: str,
    db: CatalogDB,
    admin: AdminOnly,
):
    service = CustomerService(db)
    try:
        await service.delete_customer(customer_id, admin.id)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/{customer_id}/locations", response_model=LocationResponse, status_code=201)
async def create_location(
    customer_id: str,
    data: LocationCreate,
    db: CatalogDB,
    admin: AdminOnly,
):
    service = CustomerService(db)
    try:
        location = await service.create_location(customer_id, data.location_id, data.location_name, admin.id)
        return LocationResponse.model_validate(location)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.patch("/{customer_id}/locations/{location_id}", response_model=LocationResponse)
async def update_location(
    customer_id: str,
    location_id: str,
    data: LocationUpdate,
    db: CatalogDB,
    admin: AdminOnly,
):
    service = CustomerService(db)
    try:
        location = await service.update_location(
            customer_id, location_id, data.location_id, data.location_name, admin.id
        )
        return LocationResponse.model_validate(location)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{customer_id}/locations/{location_id}", status_code=204)
async def delete_location(
    customer_id: str,
    location_id: str,
    db: CatalogDB,
    admin: AdminOnly,
):
    service = CustomerService(db)
    try:
        await service.delete_location(customer_id, location_id, admin.id)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
