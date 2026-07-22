from fastapi import APIRouter, HTTPException
from ...services.customer_service import CustomerService
from ...schemas.customer import CustomerWithLocations, LocationResponse, CustomerResponse
from ..deps import CurrentUser, CatalogDB

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


@router.get("/", response_model=list[CustomerResponse])
async def list_customers(
    db: CatalogDB,
    _: CurrentUser,
):
    service = CustomerService(db)
    customers = await service.list_customers()
    return [CustomerResponse.model_validate(c) for c in customers]
