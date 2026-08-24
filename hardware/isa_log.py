"""
ISA log-tailing driver — get counts WITHOUT touching the serial port.

ISA's BPS C1 plugin logs what it reads from the machine to a device log file
(CPS.Isa.DeviceSupport.Plugins.Common writes DEVICE_LOG.LOG in a configured
log directory). Two kinds of entry carry count data:

    Received : <raw report line>               (Debug/Trace level)
    Product Code = 10401 , Quantity = 12 ... sent to ISA.   (Info level)

The product codes are the same ISA codes carried in our device profiles
(taken from ISA's own plugin config), so either entry type is enough to
rebuild the count.

Because this driver only reads a file ISA is writing anyway, it runs happily
WHILE ISA keeps the serial port — no conflict, no cabling changes, nothing
about ISA is modified. The trade-offs: counts arrive only if ISA is running
and its logging level stays at Info or lower, and a log line format we have
not seen may need a tweak to the regexes below (capture a real log excerpt
with the machine before relying on it).

Setup (.env):
  COUNTER_MODE=isa_log
  ISA_LOG_DIR=C:\\path\\to\\isa\\device\\logs
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .base import CashCounter, CountResult
from .config import COUNTER_PROFILE, ISA_LOG_DIR, ISA_LOG_POLL_SECONDS
from .report_parser import ReportParseError, parse_report
from .report_profile import DeviceProfile, load_profile

logger = logging.getLogger(__name__)

# "Product Code = 10401 , Quantity = 12" — spacing deliberately loose.
_PRODUCT_RE = re.compile(
    r"Product\s*Code\s*=\s*(\d+)\s*,\s*Quantity\s*=\s*(\d+)", re.IGNORECASE
)
# "Received : EUR 50   12   600.00" — capture the raw payload after the marker.
_RECEIVED_RE = re.compile(r"Received\s*:\s?(.*)$", re.IGNORECASE)
# Entries that mark the end of a batch's transfer into ISA.
_BATCH_END_RE = re.compile(r"sent\s+to\s+ISA", re.IGNORECASE)

# How long the log must stay quiet after batch data before we call it complete
# (used when no explicit end marker shows up).
_IDLE_SECONDS = 2.0


class ISALogCounter(CashCounter):
    """Reads counts from the device log ISA's own plugin writes."""

    def __init__(self, profile: DeviceProfile | None = None,
                 log_dir: str | Path | None = None,
                 idle_seconds: float = _IDLE_SECONDS,
                 poll_seconds: float | None = None) -> None:
        self._profile = profile or load_profile(COUNTER_PROFILE)
        self._dir = Path(log_dir or ISA_LOG_DIR)
        self._idle = idle_seconds
        self._poll = poll_seconds if poll_seconds is not None else ISA_LOG_POLL_SECONDS
        self._file: Path | None = None
        self._pos = 0
        self._isacode_map = {
            d.isacode: d.nexus for d in self._profile.denoms if d.isacode
        }

    # ── CashCounter interface ────────────────────────────────────────────────

    def connect(self) -> bool:
        if not str(self._dir).strip():
            raise ConnectionError(
                "ISA_LOG_DIR is not set. Point it at the directory where ISA's "
                "device plugin writes its logs (the folder containing "
                "DEVICE_LOG.LOG or similar), then restart."
            )
        if not self._dir.is_dir():
            raise ConnectionError(
                f"ISA log directory {self._dir} does not exist on this PC. "
                "Check ISA_LOG_DIR in .env."
            )
        self._file = self._newest_log()
        if self._file is None:
            raise ConnectionError(
                f"No log files found in {self._dir}. Either ISA has not written "
                "any yet, or this is the wrong directory."
            )
        # Start at the end: only batches counted from now on are reported.
        self._pos = self._file.stat().st_size
        logger.info("Tailing %s from byte %d.", self._file, self._pos)
        return True

    def disconnect(self) -> None:
        self._file = None
        self._pos = 0

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        if self._file is None:
            raise RuntimeError("Not connected. Call connect() first.")

        deadline = time.monotonic() + timeout_seconds
        quantities: dict[str, int] = {}
        raw_lines: list[str] = []
        unknown_codes: dict[str, int] = {}
        last_data_at: float | None = None
        batch_ended = False

        while True:
            for line in self._read_new_lines():
                if self._ingest(line, quantities, raw_lines, unknown_codes):
                    last_data_at = time.monotonic()
                if _BATCH_END_RE.search(line) and (quantities or raw_lines):
                    batch_ended = True

            now = time.monotonic()
            have_data = bool(quantities or raw_lines)
            if have_data and (
                batch_ended or (last_data_at and now - last_data_at >= self._idle)
            ):
                return self._finish(quantities, raw_lines, unknown_codes)

            if now >= deadline:
                raise TimeoutError(
                    f"No count appeared in the ISA log within {timeout_seconds}s. "
                    "Check that ISA is running, a batch was actually counted, and "
                    "ISA's plugin logging level is Info or lower."
                )
            time.sleep(self._poll)

    # ── internals ────────────────────────────────────────────────────────────

    def _newest_log(self) -> Path | None:
        files = [p for p in self._dir.iterdir() if p.is_file()]
        return max(files, key=lambda p: p.stat().st_mtime, default=None)

    def _read_new_lines(self) -> list[str]:
        """Read newly appended text, following rotation to a newer file."""
        newest = self._newest_log()
        if newest is not None and newest != self._file:
            self._file = newest
            self._pos = 0

        try:
            size = self._file.stat().st_size
        except OSError:
            return []
        if size < self._pos:  # truncated/rewritten in place
            self._pos = 0
        if size == self._pos:
            return []

        with open(self._file, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._pos)
            chunk = fh.read()
            self._pos = fh.tell()
        return chunk.splitlines()

    def _ingest(self, line: str, quantities: dict[str, int],
                raw_lines: list[str], unknown_codes: dict[str, int]) -> bool:
        """Extract count data from one log line. True if the line carried any."""
        m = _PRODUCT_RE.search(line)
        if m:
            code, qty = m.group(1), int(m.group(2))
            nexus = self._isacode_map.get(code)
            if nexus is None:
                unknown_codes[code] = unknown_codes.get(code, 0) + qty
            else:
                quantities[nexus] = quantities.get(nexus, 0) + qty
            return True

        m = _RECEIVED_RE.search(line)
        if m and m.group(1).strip():
            raw_lines.append(m.group(1))
            return True
        return False

    def _finish(self, quantities: dict[str, int], raw_lines: list[str],
                unknown_codes: dict[str, int]) -> CountResult:
        if unknown_codes:
            logger.warning(
                "Product codes not in profile %r: %s — add them to the "
                "profile's denoms (isacode) to include them.",
                self._profile.name, unknown_codes,
            )

        # Prefer the plugin's own parsed output; it is what ISA booked.
        if quantities:
            return CountResult(denominations=dict(quantities))

        # Otherwise rebuild the report from the raw "Received :" payloads.
        text = "\n".join(raw_lines)
        try:
            report = parse_report(text, self._profile)
        except ReportParseError as exc:
            raise ReportParseError(
                f"ISA logged raw data but it did not parse: {exc}\n"
                f"Raw lines:\n{text}"
            ) from exc
        if report.warnings:
            for warning in report.warnings:
                logger.warning(warning)
        return CountResult(denominations=report.denominations,
                           raw_response=text.encode("utf-8", "replace"))
