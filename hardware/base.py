from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CountResult:
    denominations: dict[str, int]  # e.g. {"€50": 12, "€20": 30, "Coins": 5}
    session_id: str | None = None
    raw_response: bytes | None = field(default=None, repr=False)


class CashCounter(ABC):
    """Abstract interface for a banknote counting machine."""

    @abstractmethod
    def connect(self) -> bool:
        """Open the connection to the machine. Returns True on success."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection cleanly."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Non-blocking liveness check of the current connection.

        Never waits for the machine to send anything -- some devices (the
        C1 included) have no command API and only speak when they have a
        batch to report, so this can only confirm the local connection
        itself (port/cable/USB path) is still intact, not that the
        physical machine on the other end is powered on or wired right.
        """

    @abstractmethod
    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        """
        Block until the machine finishes a counting session and returns
        denomination counts. Raises TimeoutError if no result within timeout.
        """
