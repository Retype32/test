from pydantic import BaseModel, Field


class CustomerResponse(BaseModel):
    customer_id: str
    customer_name: str

    model_config = {"from_attributes": True}


class LocationResponse(BaseModel):
    location_id: str
    customer_id: str
    location_name: str

    model_config = {"from_attributes": True}


class CustomerWithLocations(BaseModel):
    customer_id: str
    customer_name: str
    locations: list[LocationResponse]

    model_config = {"from_attributes": True}


class CustomerCreate(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=50)
    customer_name: str = Field(..., min_length=1, max_length=255)


class CustomerUpdate(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=50)
    customer_name: str = Field(..., min_length=1, max_length=255)


class LocationCreate(BaseModel):
    location_id: str = Field(..., min_length=1, max_length=50)
    location_name: str = Field(..., min_length=1, max_length=255)


class LocationUpdate(BaseModel):
    location_id: str = Field(..., min_length=1, max_length=50)
    location_name: str = Field(..., min_length=1, max_length=255)
