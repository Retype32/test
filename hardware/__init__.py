from .base import CashCounter, CountResult
from .config import COUNTER_MODE


def get_counter() -> CashCounter:
    """Return the configured CashCounter implementation."""
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


__all__ = ["CashCounter", "CountResult", "get_counter"]
