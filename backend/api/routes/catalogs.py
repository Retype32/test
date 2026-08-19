from fastapi import APIRouter
from ...core.catalogs import CatalogCode, catalog_display_name
from ...schemas.catalog import CatalogInfo
from ..deps import CurrentUser

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


@router.get("/", response_model=list[CatalogInfo])
async def list_catalogs(_: CurrentUser):
    return [
        CatalogInfo(code=code.value, display_name=catalog_display_name(code))
        for code in CatalogCode
    ]
