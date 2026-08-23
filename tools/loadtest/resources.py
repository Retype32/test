"""Resource accounting for a load-test run (plan section 6 / section 9
"resources" schema): CPU + memory of the uvicorn server process via
psutil, SQLite .db file sizes and free disk space via os.statvfs.

Kept independent of the metrics/httpx layers so it can run (or be skipped
cleanly) regardless of whether the caller knows the server's PID or DB
directory -- both are optional, and their absence is reported as
"not_measured" rather than raising, since a load-test run against a server
someone else started (no PID handed to us) is still a legitimate way to use
this harness.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is expected to be present
    psutil = None


CATALOGS = ("core", "vms", "dayshift", "complete", "esnf")

_DB_FILENAMES = {
    "core": "core.db",
    "vms": "catalog_vms.db",
    "dayshift": "catalog_dayshift.db",
    "complete": "catalog_complete.db",
    "esnf": "catalog_esnf.db",
}


def db_file_sizes_mb(db_dir: Optional[str]) -> dict:
    """Sizes (MB) of the 5 SQLite database files (core + 4 catalogs), plus
    their -wal/-shm sidecars, if db_dir is known. Missing files (a catalog
    never touched yet) report 0.0, not an error."""
    sizes = {}
    for name, filename in _DB_FILENAMES.items():
        total_bytes = 0
        if db_dir:
            for suffix in ("", "-wal", "-shm"):
                path = os.path.join(db_dir, filename + suffix)
                if os.path.exists(path):
                    total_bytes += os.path.getsize(path)
        sizes[name] = round(total_bytes / (1024 * 1024), 4)
    return sizes


def disk_free_mb(db_dir: Optional[str]) -> Optional[float]:
    if not db_dir or not os.path.exists(db_dir):
        return None
    st = os.statvfs(db_dir)
    return round((st.f_bavail * st.f_frsize) / (1024 * 1024), 2)


@dataclass
class ResourceSample:
    t: float
    cpu_percent: float
    memory_mb: float


@dataclass
class ResourceSampler:
    """Samples CPU% and RSS memory of a given PID at a fixed interval on a
    background asyncio task. Call start()/stop() around the load
    generation window. If pid is None or psutil is unavailable, sampling
    is a documented no-op -- summary() still returns a well-formed dict
    with instrumentation_method == "not_measured"."""

    pid: Optional[int]
    interval_seconds: float = 2.0
    samples: list[ResourceSample] = field(default_factory=list)
    _task: Optional[asyncio.Task] = None
    _stop: bool = False
    _proc: Optional["psutil.Process"] = None

    def available(self) -> bool:
        return bool(self.pid) and psutil is not None

    async def start(self) -> None:
        if not self.available():
            return
        try:
            self._proc = psutil.Process(self.pid)
            # Prime cpu_percent(): the first call always returns 0.0/None,
            # it's a baseline for the *next* call's interval measurement.
            self._proc.cpu_percent(interval=None)
        except Exception:
            self._proc = None
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self.interval_seconds)
            if self._proc is None:
                continue
            try:
                cpu = self._proc.cpu_percent(interval=None)
                mem = self._proc.memory_info().rss / (1024 * 1024)
                self.samples.append(ResourceSample(t=time.time(), cpu_percent=cpu, memory_mb=mem))
            except Exception:
                # Process exited or became unreadable mid-run -- stop
                # sampling silently rather than crashing the load test.
                break

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval_seconds + 2)
            except asyncio.TimeoutError:
                self._task.cancel()

    def summary(self, db_dir: Optional[str] = None) -> dict:
        cpu_vals = [s.cpu_percent for s in self.samples]
        mem_vals = [s.memory_mb for s in self.samples]
        return {
            "sampled_every_seconds": self.interval_seconds,
            "cpu_percent": {
                "mean": round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else 0.0,
                "max": round(max(cpu_vals), 2) if cpu_vals else 0.0,
            },
            "memory_mb": {
                "mean": round(sum(mem_vals) / len(mem_vals), 2) if mem_vals else 0.0,
                "max": round(max(mem_vals), 2) if mem_vals else 0.0,
                "start": round(mem_vals[0], 2) if mem_vals else 0.0,
                "end": round(mem_vals[-1], 2) if mem_vals else 0.0,
            },
            "instrumentation_method": "psutil_pid" if (self.available() and self._proc) else "not_measured",
        }
