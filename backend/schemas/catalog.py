from pydantic import BaseModel


class CatalogInfo(BaseModel):
    code: str
    display_name: str
