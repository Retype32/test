from sqlalchemy.ext.asyncio import AsyncSession
from backend.core.catalogs import CatalogCode, CATALOG_DISPLAY_NAMES
from backend.services.notification_service import NotificationService
from backend.models.user import User


async def build_nav_context(current_user: User, catalog_code: CatalogCode, catalog_db: AsyncSession) -> dict:
    """Common template context every catalog-scoped page needs: identity,
    catalog label, and the notification badge count."""
    count = 0
    if current_user.role.value in ("supervisor", "administrator"):
        count = await NotificationService(catalog_db).count_open()

    return {
        "user": current_user,
        "catalog_code": catalog_code.value,
        "catalog_name": CATALOG_DISPLAY_NAMES[catalog_code],
        "notification_count": count,
    }
