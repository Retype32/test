import enum
from .config import settings


class CatalogCode(str, enum.Enum):
    vms = "vms"
    dayshift = "dayshift"
    complete = "complete"
    esnf = "esnf"


CATALOG_DISPLAY_NAMES: dict[CatalogCode, str] = {
    CatalogCode.vms: "VMS",
    CatalogCode.dayshift: "Brink's Dayshift",
    CatalogCode.complete: "Brink's Complete",
    CatalogCode.esnf: "ESNF",
}

_CATALOG_URLS: dict[CatalogCode, str] = {
    CatalogCode.vms: settings.database_url_vms,
    CatalogCode.dayshift: settings.database_url_dayshift,
    CatalogCode.complete: settings.database_url_complete,
    CatalogCode.esnf: settings.database_url_esnf,
}


def catalog_db_url(code: CatalogCode) -> str:
    return _CATALOG_URLS[code]
