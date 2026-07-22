from fastapi import APIRouter
from ...core.catalogs import CatalogCode, CATALOG_DISPLAY_NAMES
from ...schemas.catalog import CatalogInfo
from ..deps import CurrentUser

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


@router.get("/", response_model=list[CatalogInfo])
async def list_catalogs(_: CurrentUser):
    return [
        CatalogInfo(code=code.value, display_name=CATALOG_DISPLAY_NAMES[code])
        for code in CatalogCode
    ]
