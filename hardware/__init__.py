from .base import CashCounter, CountResult
from .config import COUNTER_MODE


def get_counter() -> CashCounter:
    """Return the configured CashCounter implementation."""
    if COUNTER_MODE == "tcp":
        from .gd_c1_tcp import GDC1TcpCounter
        return GDC1TcpCounter()
    if COUNTER_MODE == "serial":
        from .gd_c1_serial import GDC1SerialCounter
        return GDC1SerialCounter()
    if COUNTER_MODE == "mock":
        from .mock import MockCounter
        return MockCounter()
    if COUNTER_MODE == "none":
        raise ConnectionError("No cash counter configured. Enter counts manually.")
    raise ValueError(
        f"Unknown COUNTER_MODE={COUNTER_MODE!r}. "
        "Set COUNTER_MODE to 'tcp', 'serial', 'mock', or 'none' in .env."
    )


__all__ = ["CashCounter", "CountResult", "get_counter"]
