"""Restore databases from a backup directory created by
backend/services/backup_service.py's create_backup().

Did not exist anywhere in the codebase before this change (PG-6) -- there
was no restore path for SQLite or PostgreSQL. Both are implemented here,
dialect-branched the same way create_backup() is:

- SQLite: the backup file (`<name>.db`, written by sqlite3's online backup
  API) is copied back over the live database file. **Precondition: the
  target engine for that database must be disposed (or the whole app
  stopped) before calling this** -- copying a file out from under an open
  aiosqlite connection pool corrupts nothing on POSIX (rename/copy-then-
  replace is safe), but the live engine's already-open file descriptors
  would keep seeing the *old* data (or, for an in-place overwrite without
  a temp-file swap, a torn read) until they're recycled. This module does
  not dispose any engine itself -- that decision belongs to whatever is
  orchestrating the restore (an offline maintenance script, an admin
  action that has already called `engine.dispose()`), never silently here.
- PostgreSQL: shells out to `pg_restore` against a target database via
  `asyncio.create_subprocess_exec` -- same subprocess-safety rules as
  backup_service.py's pg_dump call: never `shell=True`, never string-
  interpolate the connection string, host/port/user/dbname as separate
  argv elements, password via the subprocess `env` (PGPASSWORD), never on
  the command line. The target database must already exist (created via
  `CREATE DATABASE` beforehand, e.g. by an operator or a test's scratch-DB
  setup) -- this module restores *into* a database, it does not create one,
  the same way `pg_restore` itself works.

CLI entrypoint: `python -m backend.services.restore_service --help`.
"""
import argparse
import asyncio
import os
import shutil
import sys

from .backup_service import _pg_conn_params, _is_postgres, _run_subprocess, _sqlite_path


def _restore_one_sqlite_sync(backup_path: str, dest_path: str) -> None:
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    # Copy to a temp file in the same directory, then atomically replace --
    # avoids leaving a half-written destination file if the copy itself is
    # interrupted partway through.
    tmp_path = dest_path + ".restoring.tmp"
    shutil.copyfile(backup_path, tmp_path)
    os.replace(tmp_path, dest_path)


async def restore_one(backup_path: str, target_url: str) -> None:
    """Restore a single database. `backup_path` is the file produced by
    create_backup() for that database (a `.db` file for SQLite, a `.dump`
    file for PostgreSQL). `target_url` is the SQLAlchemy URL of the
    database to restore into."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"backup file not found: {backup_path}")

    if _is_postgres(target_url):
        params = _pg_conn_params(target_url)
        env = dict(os.environ)
        env["PGPASSWORD"] = params["password"]
        argv = [
            "pg_restore",
            "-h", params["host"],
            "-p", params["port"],
            "-U", params["user"],
            "-d", params["dbname"],
            "--clean",
            "--if-exists",
            "--no-owner",
            backup_path,
        ]
        await _run_subprocess(argv, env)
    else:
        dest_path = _sqlite_path(target_url)
        await asyncio.to_thread(_restore_one_sqlite_sync, backup_path, dest_path)


def _backup_filename_for(name: str, backup_dir: str) -> str:
    """Backups name files `<name>.dump` (PostgreSQL) or `<name>.db`
    (SQLite) -- figure out which one is actually present for this name."""
    for ext in (".dump", ".db"):
        candidate = os.path.join(backup_dir, f"{name}{ext}")
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"no backup file for '{name}' in {backup_dir} (looked for .dump and .db)"
    )


async def restore_backup(backup_dir: str, targets: dict[str, str]) -> dict:
    """Restore every database named in `targets` (name -> target URL) from
    `backup_dir` (a directory produced by create_backup(), e.g.
    backups/20260101_120000/). Mirrors create_backup()'s per-database error
    isolation: one database's restore failure doesn't abort the rest, and
    the caller gets a structured report of what succeeded/failed."""
    restored = []
    failed = []
    for name, target_url in targets.items():
        try:
            backup_path = _backup_filename_for(name, backup_dir)
            await restore_one(backup_path, target_url)
            restored.append(name)
        except Exception as exc:  # noqa: BLE001 - see create_backup(): one
            # database's failure must not abort the others.
            failed.append({"name": name, "error": str(exc)})
    return {"dir": backup_dir, "restored": restored, "failed": failed}


def _parse_target(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(
            f"expected NAME=URL, got {spec!r}"
        )
    name, url = spec.split("=", 1)
    return name, url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore one or more databases from a backup directory "
        "created by backend.services.backup_service.create_backup()."
    )
    parser.add_argument(
        "--backup-dir", required=True,
        help="Path to a backups/<timestamp>/ directory.",
    )
    parser.add_argument(
        "--target", action="append", required=True, metavar="NAME=URL",
        help="A database to restore, e.g. "
        "core=postgresql+asyncpg://user:pass@host:5432/dbname or "
        "catalog_vms=sqlite+aiosqlite:///./catalog_vms.db. May be repeated. "
        "NAME must match a backup file's basename (core, catalog_vms, "
        "catalog_dayshift, catalog_complete, catalog_esnf).",
    )
    args = parser.parse_args(argv)

    targets = dict(_parse_target(spec) for spec in args.target)

    result = asyncio.run(restore_backup(args.backup_dir, targets))
    print(f"Restored: {', '.join(result['restored']) or '(none)'}")
    if result["failed"]:
        print("Failed:", file=sys.stderr)
        for f in result["failed"]:
            print(f"  {f['name']}: {f['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
