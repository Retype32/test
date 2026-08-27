"""
Shared, process-wide connection to the G+D BPS C1 banknote counter.

Built on the connection and framing approach proven by the standalone C1
Checker (C1 Check.py at the project root) against a real machine. The
central idea, and the reason this module looks the way it does:

    The port is opened once and listened to CONTINUOUSLY, by a background
    reader thread, for as long as the app runs.

That matters because the C1 is a printer, not a request/response device.
It prints when the operator ends a batch, on its own schedule -- which is
frequently *before* the cashier reaches the Count Cash screen. A reader
that only listens while a request is in flight silently loses those
reports: the bytes land in the OS receive buffer and are discarded by the
next read. The checker never loses one because it is always listening,
with a single long-lived StreamCollector spanning the whole session, and
this module now does the same. Reports counted between polls are queued,
not dropped.

A serial port has exactly one owner, so Nexus claims it on startup and
keeps it. Note that this cuts both ways: while C1 Check.py (or ISA) holds
the port, Nexus cannot open it, and vice versa. Run one at a time.

The startup attempt is not the only chance to get it. If the machine was
not ready when Nexus started (COM port still enumerating, ISA still
holding it for a moment), or the connection dies later (cable pulled, USB
device re-enumerated), wait_for_shared_count() reconnects on demand
instead of reporting "not connected" for the rest of the process's life
just because the very first attempt lost a race.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP
from pathlib import Path

import serial  # pyserial

from .c1_engine import (
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
    "Access is denied", which gives no hint about who the owner usually is.
    """
    detail = str(exc)
    lowered = detail.lower()

    if "access is denied" in lowered or "permission" in lowered:
        return (
            f"Serial port {port_name} is already in use by another program.\n"
            "A serial port has exactly one owner. The usual culprits are "
            "C1 Check.py still running in another window, or ISA -- its BPS C1 "
            "plugin holds the machine's printer port for as long as the plugin "
            "is loaded, which locks Nexus out.\n"
            "Close whichever one is running and try again. If ISA must stay up, "
            "the options are: give the machine a second report output on another "
            "interface if its firmware supports one, run Nexus on a separate PC, "
            "or split the line with a passive Y-cable so both hosts receive the "
            "same printout.\n"
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
    """Reads BPS C1 batch reports from the machine's serial printer port.

    connect() starts a background reader that listens continuously and
    queues every finished batch; wait_for_count_result() takes the next one
    off that queue. Nothing the machine prints while no one is waiting is
    lost.
    """

    def __init__(self) -> None:
        self._port: serial.Serial | None = None
        self._seen = SeenReports(_SEEN_REPORTS_PATH)
        self._queue: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._dead: Exception | None = None

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

        # The only buffer flush in the driver, and it happens here rather
        # than per read: whatever is sitting in the port from before Nexus
        # owned it is not ours to book. From this point on nothing is ever
        # discarded -- see the class docstring.
        self._port.reset_input_buffer()

        self._start_reader()
        logger.info("Listening on %s at %s baud.", COUNTER_COM_PORT, COUNTER_BAUD_RATE)
        return True

    def disconnect(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)
        if self._port and self._port.is_open:
            self._port.close()
            logger.info("Port closed.")
        self._port = None

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        """Return the next finished batch, waiting up to timeout_seconds.

        Batches the machine printed while nobody was waiting are already
        queued and come back immediately.
        """
        if self._reader is None:
            raise RuntimeError("Not connected. Call connect() first.")

        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._dead is not None:
                raise self._dead
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No report received within {timeout_seconds}s. Check that "
                        "the C1 report output is routed to the serial port and that "
                        "the cable and line settings match (115200 8N1, no handshake)."
                    )
                continue
            if isinstance(item, Exception):
                raise item
            return item

    # ── background reader ─────────────────────────────────────────────────

    def _start_reader(self) -> None:
        self._stop.clear()
        self._dead = None
        self._reader = threading.Thread(
            target=self._read_forever, name="c1-reader", daemon=True
        )
        self._reader.start()

    def _read_forever(self) -> None:
        """Listen continuously, framing and queueing every batch.

        One StreamCollector for the whole connection, exactly as the
        standalone checker does it: a report split across many reads is
        reassembled, and several reports arriving together are all queued
        rather than the first one winning and the rest being dropped.
        """
        collector = StreamCollector()
        while not self._stop.is_set():
            try:
                chunk = self._port.read(4096)
            except (serial.SerialException, OSError) as exc:
                # The port itself has died (cable pulled, device
                # re-enumerated). Record it so the waiting request fails
                # with the real reason and the shared connection is dropped.
                if not self._stop.is_set():
                    logger.exception("Serial port failed while listening.")
                    self._dead = serial.SerialException(str(exc))
                return

            if chunk:
                for raw in collector.add(chunk):
                    self._handle_report(raw)
            else:
                idle = collector.idle_report(_IDLE_TIMEOUT_SECONDS)
                if idle:
                    self._handle_report(idle)

    def _handle_report(self, raw: bytes) -> None:
        """Parse, validate and queue one framed report."""
        try:
            report = parse_and_validate(strip_escpos(raw))
        except ReportParseError as exc:
            # A report that fails its own arithmetic is worth surfacing to
            # the cashier -- it usually means a partial read, and silently
            # swallowing it would look like the machine never printed.
            logger.warning("Rejected a report that failed validation: %s", exc)
            self._queue.put(exc)
            return

        if self._seen.contains(report.fingerprint):
            # A re-delivered report (receipt reprint, the machine repeating
            # itself) must never be booked twice. It is also not an error
            # worth interrupting the cashier over, so it is dropped quietly.
            logger.info(
                "Ignoring report %s — already read and booked.", report.report_number or "?"
            )
            return
        self._seen.add(report.fingerprint)

        logger.info(
            "Report %s (user %s, machine SN %s): %s",
            report.report_number or "?", report.user_id or "?",
            report.machine_serial_number or "?", report.denominations,
        )

        denominations = dict(report.denominations)
        if report.coin_amount:
            # The wizard's "Coins" field is a bulk euro amount, not a piece
            # count (TransactionService.DENOMINATION_VALUES["Coins"] == 1),
            # so the coin total maps onto it directly.
            denominations["Coins"] = int(
                report.coin_amount.to_integral_value(rounding=ROUND_HALF_UP)
            )
        self._queue.put(CountResult(denominations=denominations, raw_response=raw))


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

    The lock is held only long enough to resolve (or establish) the shared
    connection -- never for the blocking wait itself. Several wizard windows
    can therefore wait at once instead of queueing behind whichever one got
    there first; the machine's batches are handed out in the order they were
    printed.
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
        # The port has died. Holding on to it would only fail the same way
        # on every future request, so drop it and let the next request open
        # a fresh connection instead of requiring a full app restart.
        with _shared_lock:
            if _shared is counter:
                counter.disconnect()
                _shared = None
        raise


__all__ = [
    "CountResult", "C1Counter", "ReportParseError",
    "get_counter", "open_shared_counter", "close_shared_counter", "wait_for_shared_count",
]
