import threading

from .base import CashCounter, CountResult
from .config import COUNTER_MODE


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
_shared: CashCounter | None = None
_shared_lock = threading.Lock()


def open_shared_counter() -> None:
    """Connect the process-wide counter. Call once from app startup."""
    global _shared
    if COUNTER_MODE == "none":
        print("[hardware] COUNTER_MODE=none — no cash counter will be connected.")
        return
    counter = get_counter()
    try:
        counter.connect()
    except Exception as exc:
        print(f"[hardware] Could not connect to the cash counter at startup: {exc}")
        print("[hardware] Continuing without it. Fix the port and restart Nexus "
              "to reconnect; counts can still be entered manually.")
        return
    _shared = counter


def close_shared_counter() -> None:
    """Release the process-wide counter. Call once from app shutdown."""
    global _shared
    if _shared is not None:
        _shared.disconnect()
        _shared = None


def wait_for_shared_count(timeout_seconds: int = 120) -> CountResult:
    """Block for one count result on the process-wide counter connection."""
    if _shared is None:
        if COUNTER_MODE == "none":
            raise ConnectionError("No cash counter is configured. Enter counts manually.")
        raise ConnectionError(
            "No cash counter is connected. Check the machine and its COM port, "
            "then restart Nexus."
        )
    with _shared_lock:
        return _shared.wait_for_count_result(timeout_seconds)


__all__ = [
    "CashCounter", "CountResult", "get_counter",
    "open_shared_counter", "close_shared_counter", "wait_for_shared_count",
]
