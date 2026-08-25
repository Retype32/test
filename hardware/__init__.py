import logging
import threading

import serial

from .base import CashCounter, CountResult
from .config import COUNTER_MODE

logger = logging.getLogger(__name__)


def get_counter() -> CashCounter:
    """Build a new CashCounter implementation instance."""
    if COUNTER_MODE in {"c1_report", "serial"}:
        from .gd_c1_report import GDC1ReportCounter
        return GDC1ReportCounter()
    if COUNTER_MODE == "isa_log":
        from .isa_log import ISALogCounter
        return ISALogCounter()
    if COUNTER_MODE == "mock":
        from .mock import MockCounter
        return MockCounter()
    if COUNTER_MODE == "none":
        raise ConnectionError("No cash counter configured. Enter counts manually.")
    raise ValueError(
        f"Unknown COUNTER_MODE={COUNTER_MODE!r}. "
        "Set COUNTER_MODE to 'c1_report', 'isa_log', 'mock', or 'none' in .env."
    )


# Opened once when the app starts and held for as long as it runs, instead of
# per transaction. A serial port has exactly one owner, so Nexus claims it
# immediately on start.bat and keeps it -- nothing else on the PC can take it
# out from under a wizard mid-count. Every read shares this one connection
# and is therefore serialised, which is fine: there is only one physical
# machine to read from regardless of how many wizard tabs are open.
#
# The startup attempt is not the only chance to get it, though. If the
# machine was not yet ready when Nexus started (COM port still enumerating,
# ISA still holding it for a moment), or the connection dies later (cable
# pulled, USB device re-enumerated), wait_for_shared_count() below reconnects
# on demand instead of reporting "not connected" for the rest of the
# process's life just because the very first attempt lost a race.
_shared: CashCounter | None = None
_shared_lock = threading.Lock()


def _connect_locked() -> CashCounter | None:
    """Try once to open a fresh counter connection. Caller holds _shared_lock."""
    counter = get_counter()
    try:
        counter.connect()
    except Exception:
        logger.exception("Could not connect to the cash counter.")
        return None
    return counter


def open_shared_counter() -> None:
    """Connect the process-wide counter. Call once from app startup."""
    global _shared
    if COUNTER_MODE == "none":
        logger.info("COUNTER_MODE=none — no cash counter will be connected.")
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
    if COUNTER_MODE == "none":
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
    "CashCounter", "CountResult", "get_counter",
    "open_shared_counter", "close_shared_counter", "wait_for_shared_count",
]
