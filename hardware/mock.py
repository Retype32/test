import logging
import random
import time

from .base import CashCounter, CountResult

logger = logging.getLogger(__name__)

_DENOMINATIONS = ["€500", "€200", "€100", "€50", "€20", "€10", "€5", "Coins"]


class MockCounter(CashCounter):
    """
    Development mock — simulates a 3-second counting session with random
    denomination counts. No hardware required.
    """

    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> bool:
        self._connected = True
        logger.info("Connected (simulated).")
        return True

    def disconnect(self) -> None:
        self._connected = False
        logger.info("Disconnected (simulated).")

    def is_connected(self) -> bool:
        return self._connected

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        logger.info("Counting banknotes… (3 s simulated delay)")
        time.sleep(3)
        denominations = {d: random.randint(0, 20) for d in _DENOMINATIONS}
        logger.info("Result: %s", denominations)
        return CountResult(denominations=denominations, session_id="MOCK-001")
