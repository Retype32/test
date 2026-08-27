"""
Shared, process-wide connection to the G+D BPS C1 banknote counter.

Built on the connection and framing approach proven by the standalone C1
Checker (C1 Check.py at the project root) against a real machine: open the
machine's serial printer port directly at 115200 8N1 no handshake, never
write a byte to it, frame each report by its own "NO:"/"ESC i" markers, and
never trust a report whose printed totals disagree with its own
denomination lines. See hardware/c1_engine.py for that logic.

Opened once when the app starts and held for as long as it runs, instead of
per transaction. A serial port has exactly one owner, so Nexus claims it
immediately on start.bat and keeps it -- nothing else on the PC can take it
out from under a wizard mid-count. Every read shares this one connection
and is therefore serialised, which is fine: there is only one physical
machine to read from regardless of how many wizard tabs are open.

The startup attempt is not the only chance to get it, though. If the
machine was not yet ready when Nexus started (COM port still enumerating,
ISA still holding it for a moment), or the connection dies later (cable
pulled, USB device re-enumerated), wait_for_shared_count() below reconnects
on demand instead of reporting "not connected" for the rest of the
process's life just because the very first attempt lost a race.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP
from pathlib import Path

import serial  # pyserial

from .c1_engine import (
    DuplicateReportError,
    ReportParseError,
    SeenReports,
    StreamCollector,
    parse_and_validate,
    strip_escpos,
)
from .config import COUNTER_BAUD_RATE, COUNTER_COM_PORT

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_SECONDS = 2.0

# Persisted so a report already booked in a previous run of the app is still
# recognised as a duplicate after a restart, not just within one process.
_SEEN_REPORTS_PATH = Path(__file__).resolve().parent.parent / "logs" / "c1_seen_reports.json"


@dataclass
class CountResult:
    denominations: dict[str, int]  # e.g. {"€50": 12, "€20": 30, "Coins": 5}
    session_id: str | None = None
    raw_response: bytes | None = field(default=None, repr=False)


def _open_failure_message(port_name: str, exc: Exception) -> str:
    """Explain a failed port open in terms of what actually goes wrong on site.

    Windows reports a port that another program already owns as a bare
    "Access is denied", which gives no hint that ISA is usually the owner.
    """
    detail = str(exc)
    lowered = detail.lower()

    if "access is denied" in lowered or "permission" in lowered:
        return (
            f"Serial port {port_name} is already in use by another program.\n"
            "A serial port has exactly one owner. If ISA is running on this PC "
            "it holds the machine's printer port open for as long as its BPS C1 "
            "plugin is loaded, which locks Nexus out.\n"
            "Options: close ISA, give the machine a second report output on "
            "another interface if its firmware supports one, run Nexus on a "
            "separate PC, or split the line with a passive Y-cable so both "
            "hosts receive the same printout.\n"
            f"Original error: {detail}"
        )

    if "could not open port" in lowered or "filenotfound" in lowered:
        return (
            f"Serial port {port_name} does not exist on this PC.\n"
            "Run 'python \"C1 Check.py\" --list-ports' to see the available "
            "ports, then set COUNTER_COM_PORT in .env to the right one.\n"
            f"Original error: {detail}"
        )

    return f"Cannot open serial port {port_name} — {detail}"


class C1Counter:
    """Reads BPS C1 batch reports from the machine's serial printer port."""

    def __init__(self) -> None:
        self._port: serial.Serial | None = None
        self._seen = SeenReports(_SEEN_REPORTS_PATH)

    def connect(self) -> bool:
        try:
            self._port = serial.Serial(
                port=COUNTER_COM_PORT,
                baudrate=COUNTER_BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
                timeout=0.10,
                write_timeout=0.5,
            )
        except serial.SerialException as exc:
            self._port = None
            raise ConnectionError(_open_failure_message(COUNTER_COM_PORT, exc)) from exc

        # A plain listener never asserts RTS/DTR itself, and some C1 units
        # hold their report output back while those lines read asserted.
        # This is a read-only driver -- it never writes a command byte -- so
        # forcing both low costs nothing and matches the wiring proven
        # against a real machine.
        self._port.rts = False
        self._port.dtr = False
        self._port.reset_input_buffer()
        logger.info("Listening on %s at %s baud.", COUNTER_COM_PORT, COUNTER_BAUD_RATE)
        return True

    def disconnect(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
            logger.info("Port closed.")
        self._port = None

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        raw = self._read_raw_report(timeout_seconds)
        text = strip_escpos(raw)
        report = parse_and_validate(text)

        if self._seen.contains(report.fingerprint):
            raise DuplicateReportError(
                f"Report {report.report_number or '?'} has already been read. "
                "The machine may be re-sending the same batch (a receipt "
                "reprint, or a repeated poll) -- run a fresh batch on the "
                "machine if you meant to count something new."
            )
        self._seen.add(report.fingerprint)

        if report.report_number:
            logger.info(
                "Report %s (user %s, machine SN %s)",
                report.report_number, report.user_id or "?", report.machine_serial_number or "?",
            )

        denominations = dict(report.denominations)
        if report.coin_amount:
            # The wizard's "Coins" field is a bulk euro amount, not a piece
            # count (TransactionService.DENOMINATION_VALUES["Coins"] == 1),
            # so the coin total maps onto it directly.
            denominations["Coins"] = int(
                report.coin_amount.to_integral_value(rounding=ROUND_HALF_UP)
            )

        return CountResult(denominations=denominations, raw_response=raw)

    # ── internals ─────────────────────────────────────────────────────────

    def _read_raw_report(self, timeout_seconds: int) -> bytes:
        """Block until one full report has been received, framed by
        StreamCollector's "NO:"/"ESC i" markers with an idle-timeout fallback."""
        if not self._port or not self._port.is_open:
            raise RuntimeError("Not connected. Call connect() first.")

        # Each call is meant to capture exactly one fresh batch. Drop
        # anything still sitting in the port's receive buffer from before
        # this call -- trailing bytes a previous report's burst left behind
        # would otherwise prefix this read and desync every batch after it.
        self._port.reset_input_buffer()

        collector = StreamCollector()
        deadline = time.monotonic() + timeout_seconds

        while True:
            chunk = self._port.read(4096)
            if chunk:
                reports = collector.add(chunk)
                if reports:
                    return reports[0]
            else:
                idle = collector.idle_report(_IDLE_TIMEOUT_SECONDS)
                if idle:
                    return idle

            if time.monotonic() >= deadline:
                partial = collector.final_partial()
                if partial:
                    logger.warning("Timeout with %d bytes buffered; parsing what arrived.", len(partial))
                    return partial
                raise TimeoutError(
                    f"No report received within {timeout_seconds}s. Check that "
                    "the C1 report output is routed to the serial port and that "
                    "the cable and line settings match (115200 8N1, no handshake)."
                )


# ── process-wide shared singleton ───────────────────────────────────────────

_shared: C1Counter | None = None
_shared_lock = threading.Lock()


def get_counter() -> C1Counter:
    """Build a new counter instance. A separate function (rather than
    constructing C1Counter() inline everywhere) so tests can substitute a
    fake without touching global state."""
    return C1Counter()


def _connect_locked() -> C1Counter | None:
    """Try once to open a fresh counter connection. Caller holds _shared_lock.

    Construction and connect() are both inside this try/except -- a bad
    connection attempt of any kind degrades to "not connected" instead of
    crashing the app's startup.
    """
    try:
        counter = get_counter()
        counter.connect()
    except Exception:
        logger.exception("Could not connect to the cash counter.")
        return None
    return counter


def open_shared_counter() -> None:
    """Connect the process-wide counter. Call once from app startup."""
    global _shared
    if not COUNTER_COM_PORT:
        logger.info("COUNTER_COM_PORT is not set — no cash counter will be connected.")
        return
    with _shared_lock:
        _shared = _connect_locked()
        if _shared is None:
            logger.warning("Continuing without it. The next count request will "
                            "retry the connection automatically.")


def close_shared_counter() -> None:
    """Release the process-wide counter. Call once from app shutdown."""
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.disconnect()
            _shared = None


def wait_for_shared_count(timeout_seconds: int = 120) -> CountResult:
    """Block for one count result on the process-wide counter connection.

    Reconnects on demand if the shared connection was never established or
    has since gone stale, so a machine that is genuinely connected now isn't
    reported as unreachable just because an earlier attempt was not.
    """
    global _shared
    if not COUNTER_COM_PORT:
        raise ConnectionError("No cash counter is configured. Enter counts manually.")

    with _shared_lock:
        if _shared is None:
            _shared = _connect_locked()
            if _shared is None:
                raise ConnectionError(
                    "No cash counter is connected. Check the machine and its COM port, "
                    "then try again."
                )
        counter = _shared
        try:
            return counter.wait_for_count_result(timeout_seconds)
        except serial.SerialException:
            # The port itself has died (cable pulled, device re-enumerated).
            # Holding on to it would only fail the same way on every future
            # request, so drop it and let the next request open a fresh
            # connection instead of requiring a full app restart.
            counter.disconnect()
            _shared = None
            raise


__all__ = [
    "CountResult", "C1Counter", "ReportParseError", "DuplicateReportError",
    "get_counter", "open_shared_counter", "close_shared_counter", "wait_for_shared_count",
]
