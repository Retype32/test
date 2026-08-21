"""Nightly automatic EOD close, run for every catalog (FI) at 00:00.

The business day that just ended (i.e. "yesterday" relative to the moment
this fires, just after midnight) is closed automatically for each catalog.
A day already closed manually earlier is left as-is — close_day rejects an
already-closed day and this job just moves on to the next catalog. Manual
close/reopen through the EOD page keeps working exactly as before; this only
adds an automatic trigger on top.
"""
from datetime import datetime, timedelta

from ..core.catalogs import CatalogCode
from ..core.database import get_catalog_sessionmaker
from .eod_service import EODService


async def auto_close_all_catalogs() -> None:
    business_date = datetime.now().date() - timedelta(days=1)

    for code in CatalogCode:
        sessionmaker = get_catalog_sessionmaker(code)
        async with sessionmaker() as session:
            try:
                service = EODService(session)
                await service.close_day(business_date, closed_by_user_id=None, automatic=True)
                await session.commit()
            except ValueError:
                # Already closed (e.g. a supervisor closed it manually
                # earlier today) — nothing to do for this catalog.
                await session.rollback()
            except Exception:
                await session.rollback()
                raise
