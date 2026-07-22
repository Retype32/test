from pydantic import BaseModel


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
