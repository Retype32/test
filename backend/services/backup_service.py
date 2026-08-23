"""Admin-triggered backup of all 5 databases (core + 4 catalogs).

Dialect-branches per database URL:

- SQLite: uses sqlite3's own online backup API (Connection.backup) rather
  than a plain file copy -- the app holds these databases open via aiosqlite
  for the whole process lifetime, so a raw copy could land mid-write. The
  backup API is safe to run against a live database. Written as
  ``<name>.db`` files, unchanged from the original SQLite-only version of
  this module.
- PostgreSQL: shells out to ``pg_dump`` in custom format (``-Fc``, the
  format ``pg_restore`` expects) via ``asyncio.create_subprocess_exec`` --
  never ``shell=True``, and the connection string is never string-
  interpolated into a shell command. Host/port/user/dbname are passed as
  separate argv elements; the password goes through the subprocess's own
  ``env`` (``PGPASSWORD``), never on the command line, so it never shows up
  in ``ps``. Written as ``<name>.dump`` files, alongside the SQLite ones
  when both kinds are present in the same run (mixed core/catalog dialects
  are supported even though today's deployment uses one dialect for all 5).

Per PG-6 (docs/production_readiness/04_postgresql_and_reconciliation.md):
sqlite3.Connection.backup() has no PostgreSQL equivalent in the standard
library, so a straight port was never possible -- this is a full rewrite,
not an extension, on the PostgreSQL side.

Per Agent 5 finding #12: a filesystem/subprocess error on one database no
longer aborts the whole backup run. Each database is attempted
independently; failures are collected into the returned ``failed`` list
instead of propagating and losing whatever backups already succeeded.
NOTE for the route that calls create_backup() (web/routes/admin_web.py,
not owned by this change): it currently only reads ``result["copied"]`` for
its audit-log message and success banner. It does not yet surface
``result["failed"]`` to the admin. That UI change is a small follow-up
outside this module's ownership.
"""
import os
import sqlite3
import asyncio
from datetime import datetime

from sqlalchemy.engine import make_url

from ..core.config import settings
from ..core.catalogs import CatalogCode, catalog_db_url

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKUP_ROOT = os.path.join(_PROJECT_ROOT, "backups")


def _sqlite_path(url: str) -> str:
    # All-SQLite URLs look like "sqlite+aiosqlite:///<path>" (three slashes --
    # relative to the process's cwd, same as SQLAlchemy resolves them when
    # the app opens them at startup).
    path = url.split("///", 1)[1]
    return os.path.abspath(path)


def _pg_conn_params(url: str) -> dict:
    """Extract host/port/user/password/dbname from a
    postgresql(+asyncpg)://user:pass@host:port/dbname URL using SQLAlchemy's
    own URL parser (handles escaping correctly -- no manual string surgery
    on a value that may contain user-controlled characters)."""
    u = make_url(url)
    return {
        "host": u.host or "localhost",
        "port": str(u.port or 5432),
        "user": u.username or "",
        "password": u.password or "",
        "dbname": u.database or "",
    }


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def _sources() -> dict[str, str]:
    sources = {"core": settings.database_url_core}
    for code in CatalogCode:
        sources[f"catalog_{code.value}"] = catalog_db_url(code)
    return sources


def _backup_one_sqlite_sync(src_path: str, dst_path: str) -> None:
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def _run_subprocess(argv: list[str], env: dict) -> None:
    """Run argv via exec (never a shell), raising RuntimeError with captured
    stderr on a non-zero exit. No part of the connection string is ever
    passed through a shell -- host/port/user/dbname are separate argv
    elements and the password travels only via `env`."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"{argv[0]} exited {proc.returncode}: {stderr.decode(errors='replace').strip()}"
        )


async def _backup_one_postgres(url: str, dst_path: str) -> None:
    params = _pg_conn_params(url)
    env = dict(os.environ)
    env["PGPASSWORD"] = params["password"]
    argv = [
        "pg_dump",
        "-h", params["host"],
        "-p", params["port"],
        "-U", params["user"],
        "-Fc",  # custom format, what pg_restore expects
        "-f", dst_path,
        params["dbname"],
    ]
    await _run_subprocess(argv, env)


async def create_backup(sources: dict[str, str] | None = None) -> dict:
    """Back up every database in `sources` (default: the app's real core +
    4 catalog databases, from settings) into a fresh backups/<timestamp>/
    directory. `sources` is overridable so this can be exercised in tests
    against scratch databases without touching the app's real settings.

    Returns {"dir", "timestamp", "copied": [names], "skipped": [names],
    "failed": [{"name", "error"}]}. A per-database failure is caught and
    recorded, never allowed to abort the remaining databases in the run.
    """
    if sources is None:
        sources = _sources()

    os.makedirs(BACKUP_ROOT, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(dest_dir, exist_ok=True)

    copied = []
    skipped = []
    failed = []

    for name, url in sources.items():
        try:
            if _is_postgres(url):
                dst_path = os.path.join(dest_dir, f"{name}.dump")
                await _backup_one_postgres(url, dst_path)
                copied.append(name)
            else:
                src_path = _sqlite_path(url)
                if not os.path.exists(src_path):
                    skipped.append(name)
                    continue
                dst_path = os.path.join(dest_dir, f"{name}.db")
                await asyncio.to_thread(_backup_one_sqlite_sync, src_path, dst_path)
                copied.append(name)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: one bad
            # database (permission error, disk full, pg_dump crash, whatever)
            # must never take down the rest of the backup run.
            failed.append({"name": name, "error": str(exc)})

    return {
        "dir": dest_dir,
        "timestamp": stamp,
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
    }


_BACKUP_EXTENSIONS = (".db", ".dump")


def list_backups() -> list[dict]:
    if not os.path.isdir(BACKUP_ROOT):
        return []

    backups = []
    for entry in sorted(os.listdir(BACKUP_ROOT), reverse=True):
        full_path = os.path.join(BACKUP_ROOT, entry)
        if not os.path.isdir(full_path):
            continue
        files = [f for f in os.listdir(full_path) if f.endswith(_BACKUP_EXTENSIONS)]
        total_size = sum(os.path.getsize(os.path.join(full_path, f)) for f in files)
        backups.append({
            "name": entry,
            "file_count": len(files),
            "total_size_bytes": total_size,
        })
    return backups
