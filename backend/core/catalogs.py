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

# Shown instead of CATALOG_DISPLAY_NAMES while settings.branding_enabled is
# off, so a catalog name never leaks the product's real name either.
_ANONYMOUS_CATALOG_DISPLAY_NAMES: dict[CatalogCode, str] = {
    CatalogCode.vms: "VMS",
    CatalogCode.dayshift: "Dayshift",
    CatalogCode.complete: "Complete",
    CatalogCode.esnf: "ESNF",
}


def catalog_display_name(code: CatalogCode) -> str:
    names = CATALOG_DISPLAY_NAMES if settings.branding_enabled else _ANONYMOUS_CATALOG_DISPLAY_NAMES
    return names[code]

_CATALOG_URLS: dict[CatalogCode, str] = {
    CatalogCode.vms: settings.database_url_vms,
    CatalogCode.dayshift: settings.database_url_dayshift,
    CatalogCode.complete: settings.database_url_complete,
    CatalogCode.esnf: settings.database_url_esnf,
}


def catalog_db_url(code: CatalogCode) -> str:
    return _CATALOG_URLS[code]
