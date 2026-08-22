import asyncio
import argparse
import contextlib
import os
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker, AsyncEngine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import AsyncAdaptedQueuePool
from .config import settings
from .catalogs import CatalogCode, catalog_db_url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _connect_args(url: str) -> dict:
    return {"check_same_thread": False} if _is_sqlite(url) else {}


def _pool_kwargs(url: str, size: int) -> dict:
    # SQLAlchemy's sqlite+aiosqlite dialect defaults to NullPool (a fresh
    # connection per checkout, no reuse) rather than pooling connections at
    # all. Once WAL mode (below) lets multiple readers run alongside the one
    # writer instead of queuing behind it, an actual connection pool lets
    # that concurrency be used instead of paying full connection-open cost
    # on every request. `size` should be at least as big as this engine's
    # concurrency-limit semaphore (below) -- otherwise the pool itself
    # becomes the bottleneck before that semaphore ever matters.
    if not _is_sqlite(url):
        return {}
    return {"poolclass": AsyncAdaptedQueuePool, "pool_size": size, "max_overflow": size // 2}


def _configure_sqlite_engine(engine: AsyncEngine) -> None:
    """SQLite defaults to a rollback journal, which locks the whole database
    file for the duration of a write and makes any second writer fail
    immediately with "database is locked" rather than wait. Under real
    concurrency (many cashiers entering transactions at once) that shows up
    as request failures well before the box runs out of CPU or memory.

    WAL mode lets readers proceed concurrently with the one active writer
    instead of blocking on it, and PRAGMA busy_timeout makes a writer that
    genuinely has to wait for another writer queue for that long instead of
    erroring out instantly. This alone is not enough at real concurrency,
    though: busy_timeout arbitrates contention between however many writer
    threads show up, and letting dozens pile onto one file lock at once (one
    OS thread per aiosqlite connection) creates a thundering herd that's
    *slower* than fast-failing was, not faster -- see
    _CATALOG_DB_CONCURRENCY_LIMIT / _CORE_DB_CONCURRENCY_LIMIT below, which
    are what actually keep this bounded.

    synchronous=NORMAL is the standard pairing with WAL: still safe against
    an application crash (the failure mode that matters here), it only
    trades away durability against an OS crash / power loss between WAL
    checkpoints -- the accepted tradeoff for this mode everywhere it's used.
    """
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


# Caps how many requests can hold a live session against one SQLite database
# at the same time -- reads and writes alike. Without this, concurrency
# beyond what SQLite's single writer can actually drain doesn't queue
# politely; it turns into dozens of OS threads all retrying the same file
# lock via busy_timeout at once, which measured *worse* than no fix at all
# (higher latency, more timeouts) once concurrency passed this level. An
# in-process asyncio.Semaphore queues fairly and cheaply instead. Not needed
# for Postgres, which doesn't share SQLite's single-writer limitation.
#
# Each catalog only ever serves its own users, so 30-40 comfortably covers
# this app's per-catalog target. core.db is different: every catalog's users
# share it (it holds accounts and login/audit history), so a login burst
# across every FI at once lands there together -- capping it at the same 30
# measured a handful of failures under 4 FIs x 20 users logging in
# simultaneously (80 at once). Sized higher accordingly; its transactions
# are also much cheaper (one read, one small audit-log insert) than a
# catalog write, so a bigger cap here is cheap.
_CATALOG_DB_CONCURRENCY_LIMIT = 30
_CORE_DB_CONCURRENCY_LIMIT = 120


class CoreBase(DeclarativeBase):
    pass


class CatalogBase(DeclarativeBase):
    pass


core_engine = create_async_engine(
    settings.database_url_core,
    echo=settings.debug,
    connect_args=_connect_args(settings.database_url_core),
    **_pool_kwargs(settings.database_url_core, size=_CORE_DB_CONCURRENCY_LIMIT),
)
if _is_sqlite(settings.database_url_core):
    _configure_sqlite_engine(core_engine)

_core_db_semaphore = asyncio.Semaphore(_CORE_DB_CONCURRENCY_LIMIT) if _is_sqlite(settings.database_url_core) else None

CoreSessionLocal = async_sessionmaker(
    core_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

_catalog_engines: dict[CatalogCode, AsyncEngine] = {}
_catalog_sessionmakers: dict[CatalogCode, async_sessionmaker] = {}
_catalog_db_semaphores: dict[CatalogCode, asyncio.Semaphore] = {}

for _code in CatalogCode:
    _url = catalog_db_url(_code)
    _engine = create_async_engine(
        _url, echo=settings.debug, connect_args=_connect_args(_url),
        **_pool_kwargs(_url, size=_CATALOG_DB_CONCURRENCY_LIMIT),
    )
    if _is_sqlite(_url):
        _configure_sqlite_engine(_engine)
        _catalog_db_semaphores[_code] = asyncio.Semaphore(_CATALOG_DB_CONCURRENCY_LIMIT)
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


async def checkpoint_all_sqlite_databases() -> None:
    """Backstop for SQLite's own auto-checkpoint, which runs in PASSIVE mode
    and quietly does nothing whenever an overlapping reader is holding an
    older WAL snapshot -- exactly the situation under sustained concurrent
    load. Without this, the WAL file can grow unbounded under a heavy
    sustained write burst, and every read/write gets progressively slower as
    it does (reproduced directly by loadtest/batch_transactions.py). Running
    an explicit PASSIVE checkpoint often (see backend/main.py's scheduler)
    keeps the WAL small continuously instead of hoping SQLite finds a quiet
    moment on its own; PASSIVE never blocks a concurrent reader or writer,
    it just checkpoints whatever it can without waiting."""
    for engine in (core_engine, *_catalog_engines.values()):
        if not _is_sqlite(str(engine.url)):
            continue
        async with engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA wal_checkpoint")


async def get_core_db() -> AsyncSession:
    async with (_core_db_semaphore or contextlib.nullcontext()):
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
    async with (_catalog_db_semaphores.get(code) or contextlib.nullcontext()):
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
