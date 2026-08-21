"""Admin-triggered backup of all 5 SQLite databases (core + 4 catalogs).

Uses sqlite3's own online backup API (Connection.backup) rather than a plain
file copy: the app holds these databases open via aiosqlite for the whole
process lifetime, so a raw copy could land mid-write. The backup API is safe
to run against a live database.
"""
import os
import sqlite3
import asyncio
from datetime import datetime

from ..core.config import settings
from ..core.catalogs import CatalogCode, catalog_db_url

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKUP_ROOT = os.path.join(_PROJECT_ROOT, "backups")


def _sqlite_path(url: str) -> str:
    # All 5 database URLs are "sqlite+aiosqlite:///<path>" (three slashes —
    # relative to the process's cwd, same as SQLAlchemy resolves them when
    # the app opens them at startup).
    path = url.split("///", 1)[1]
    return os.path.abspath(path)


def _sources() -> dict[str, str]:
    sources = {"core": _sqlite_path(settings.database_url_core)}
    for code in CatalogCode:
        sources[f"catalog_{code.value}"] = _sqlite_path(catalog_db_url(code))
    return sources


def _backup_one_sync(src_path: str, dst_path: str) -> None:
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


async def create_backup() -> dict:
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(dest_dir, exist_ok=True)

    copied = []
    skipped = []
    for name, src_path in _sources().items():
        if not os.path.exists(src_path):
            skipped.append(name)
            continue
        dst_path = os.path.join(dest_dir, f"{name}.db")
        await asyncio.to_thread(_backup_one_sync, src_path, dst_path)
        copied.append(name)

    return {"dir": dest_dir, "timestamp": stamp, "copied": copied, "skipped": skipped}


def list_backups() -> list[dict]:
    if not os.path.isdir(BACKUP_ROOT):
        return []

    backups = []
    for entry in sorted(os.listdir(BACKUP_ROOT), reverse=True):
        full_path = os.path.join(BACKUP_ROOT, entry)
        if not os.path.isdir(full_path):
            continue
        files = [f for f in os.listdir(full_path) if f.endswith(".db")]
        total_size = sum(os.path.getsize(os.path.join(full_path, f)) for f in files)
        backups.append({
            "name": entry,
            "file_count": len(files),
            "total_size_bytes": total_size,
        })
    return backups
