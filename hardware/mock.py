import random
import time

from .base import CashCounter, CountResult

_DENOMINATIONS = ["€500", "€200", "€100", "€50", "€20", "€10", "€5", "Coins"]


class MockCounter(CashCounter):
    """
    Development mock — simulates a 3-second counting session with random
    denomination counts. No hardware required.
    """

    def connect(self) -> bool:
        print("[MockCounter] Connected (simulated).")
        return True

    def disconnect(self) -> None:
        print("[MockCounter] Disconnected (simulated).")

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        print("[MockCounter] Counting banknotes… (3 s simulated delay)")
        time.sleep(3)
        denominations = {d: random.randint(0, 20) for d in _DENOMINATIONS}
        print(f"[MockCounter] Result: {denominations}")
        return CountResult(denominations=denominations, session_id="MOCK-001")
