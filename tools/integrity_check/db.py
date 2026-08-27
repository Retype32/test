"""Connection resolution and read-only connection handling.

URL resolution deliberately imports and reuses `backend.core.config.settings`
/ `backend.core.catalogs.catalog_db_url` rather than re-parsing
`DATABASE_URL_*` env vars itself -- those two modules already implement the
app's own "env var, falling back to a sqlite default" resolution (pydantic
`BaseSettings` reads `DATABASE_URL_CORE`/`DATABASE_URL_VMS`/etc. from the
environment at instantiation time), so `--catalog vms` with no
`--catalog-database-url` picks up exactly what the app itself would use.
This is read-only importing of app config, not a write dependency -- no
`backend.core.database` engine this tool creates is one the app shares or
touches.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

# Reused, not reimplemented -- see module docstring. Importing
# backend.core.database also builds the app's own 5 engines at import time
# (module-level side effect that already exists in that module regardless of
# this tool); that's inert until something executes against them, and this
# tool never does -- it only borrows `_connect_args` for the SQLite
# `check_same_thread` kwarg.
from backend.core.database import _connect_args  # noqa: E402


def resolve_catalog_url(cli_value: Optional[str], catalog_code: str) -> str:
    if cli_value:
        return cli_value
    from backend.core.catalogs import CatalogCode, catalog_db_url

    return catalog_db_url(CatalogCode(catalog_code))


def resolve_core_url(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    from backend.core.config import settings

    return settings.database_url_core


def redact_url(url: str, *, redact: bool) -> str:
    """Strips credentials before a connection string ever reaches output.

    Network dialects (postgresql+asyncpg://user:pass@host:port/db) collapse
    the whole userinfo section to `***`, matching the example in
    04_postgresql_and_reconciliation.md §4.3. SQLite URLs carry no
    credentials (they're a bare file path) so there is nothing to redact --
    returned unchanged either way.
    """
    if not redact:
        return url
    try:
        u = make_url(url)
    except Exception:
        return url
    if u.host is None:
        return url  # sqlite:///path -- no credentials to strip
    netloc = u.host if u.port is None else f"{u.host}:{u.port}"
    return f"{u.drivername}://***@{netloc}/{u.database or ''}"


class ConnectionError_(Exception):
    """Raised for any failure opening/preparing a read-only connection --
    caught at the top of the CLI and turned into exit code 2 (tool/
    connection error), never exit code 1 (a found violation)."""


@contextlib.asynccontextmanager
async def open_readonly_connection(url: str) -> AsyncIterator[AsyncConnection]:
    """Opens one connection, hardened read-only per §4.1's design, and
    disposes its throwaway engine on the way out.

    PostgreSQL: AUTOCOMMIT isolation + `SET SESSION CHARACTERISTICS AS
    TRANSACTION READ ONLY` -- stronger than a single `SET TRANSACTION READ
    ONLY` (which only binds the very next auto-committed transaction under
    AUTOCOMMIT): the SESSION-level form applies to every transaction opened
    on this connection for the rest of its life, so a check that runs
    several statements is protected throughout, not just for its first one.
    Any write statement is rejected by the server itself
    (`cannot execute ... in a read-only transaction`), independent of
    whatever this tool's Python code does or doesn't call.

    SQLite has no equivalent read-only *transaction* mode to request -- it
    is a single-user embedded engine with no server-side transaction
    isolation GUCs. `PRAGMA query_only = ON` is the closest real mechanism
    (the driver itself rejects any write statement at the connection level),
    and is applied here as genuine defense-in-depth, but it is NOT the same
    guarantee as PostgreSQL's: it's a connection-level pragma, not a
    transactional property, so nothing here should be read as SQLite
    getting identical protection to PostgreSQL. Absent this pragma the only
    protection would be "the tool's code just never calls
    .flush()/.commit()/.add()" -- true, and still the ultimate backstop, but
    strictly weaker than what PostgreSQL enforces server-side.
    """
    engine = create_async_engine(url, connect_args=_connect_args(url))
    try:
        try:
            conn = await engine.connect()
        except Exception as exc:
            raise ConnectionError_(f"could not connect to {url!r}: {exc}") from exc
        try:
            try:
                if url.startswith("sqlite"):
                    await conn.exec_driver_sql("PRAGMA query_only = ON")
                else:
                    await conn.execution_options(isolation_level="AUTOCOMMIT")
                    await conn.exec_driver_sql(
                        "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
                    )
            except Exception as exc:
                raise ConnectionError_(
                    f"could not set up a read-only session against {url!r}: {exc}"
                ) from exc
            yield conn
        finally:
            await conn.close()
    finally:
        await engine.dispose()


@dataclass
class RunContext:
    """Everything one check function needs. Bound to plain
    `AsyncConnection`s (SQLAlchemy Core), not ORM `Session`s -- a Core
    connection has no `.add()`/no unit-of-work, so "read-only by
    construction" is structural, not just a convention this tool's code
    happens to follow."""

    catalog_conn: AsyncConnection
    core_conn: Optional[AsyncConnection]
    catalog_code: str
    date_from: Optional["object"]
    date_to: Optional["object"]
    sample_size: int

    def date_clauses(self, column):
        clauses = []
        if self.date_from is not None:
            clauses.append(column >= self.date_from)
        if self.date_to is not None:
            clauses.append(column <= self.date_to)
        return clauses
