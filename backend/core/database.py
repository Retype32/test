import asyncio
import argparse
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker, AsyncEngine
from sqlalchemy.orm import DeclarativeBase
from .config import settings
from .catalogs import CatalogCode, catalog_db_url


def _connect_args(url: str) -> dict:
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


class CoreBase(DeclarativeBase):
    pass


class CatalogBase(DeclarativeBase):
    pass


core_engine = create_async_engine(
    settings.database_url_core,
    echo=settings.debug,
    connect_args=_connect_args(settings.database_url_core),
)

CoreSessionLocal = async_sessionmaker(
    core_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

_catalog_engines: dict[CatalogCode, AsyncEngine] = {}
_catalog_sessionmakers: dict[CatalogCode, async_sessionmaker] = {}

for _code in CatalogCode:
    _url = catalog_db_url(_code)
    _engine = create_async_engine(_url, echo=settings.debug, connect_args=_connect_args(_url))
    _catalog_engines[_code] = _engine
    _catalog_sessionmakers[_code] = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


def get_catalog_sessionmaker(code: CatalogCode) -> async_sessionmaker:
    return _catalog_sessionmakers[code]


async def get_core_db() -> AsyncSession:
    async with CoreSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_catalog_db(code: CatalogCode) -> AsyncSession:
    async with get_catalog_sessionmaker(code)() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stamp_head_sync(ini_filename: str, x_args: list[str]):
    from alembic.config import Config
    from alembic import command

    cfg = Config(os.path.join(_PROJECT_ROOT, ini_filename))
    cfg.cmd_opts = argparse.Namespace(x=x_args)
    command.stamp(cfg, "head")


async def init_databases():
    """Create any missing tables, then stamp each database as being at the
    latest Alembic revision. Table creation stays on metadata.create_all for
    a zero-config first run; the stamp keeps Alembic's own bookkeeping in
    sync so a later `alembic revision --autogenerate` never gets confused by
    a database it doesn't recognize (e.g. after a dev deletes a .db file)."""
    async with core_engine.begin() as conn:
        await conn.run_sync(CoreBase.metadata.create_all)
    await asyncio.to_thread(_stamp_head_sync, "alembic.ini", [])

    for code, engine in _catalog_engines.items():
        async with engine.begin() as conn:
            await conn.run_sync(CatalogBase.metadata.create_all)
        await asyncio.to_thread(_stamp_head_sync, "alembic_catalog.ini", [f"catalog={code.value}"])
