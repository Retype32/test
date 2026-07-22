"""
G&D BPS C1 — TCP/LAN driver

The BPS C1 exposes a proprietary TCP interface used by BPS Connect and
Compass VMS software. The exact command/response framing is not publicly
documented by Giesecke+Devrient.

To activate this driver:
  1. Obtain the G&D integration SDK / technical specification from your
     G&D account manager or via gi-de.com support. (Issued under NDA to
     integration partners.)  Alternatively, capture BPS Connect ↔ C1
     traffic with Wireshark on the LAN to reverse-engineer the frames.
  2. Replace the TODO sections below with the real byte sequences.
  3. Set COUNTER_MODE=tcp, COUNTER_HOST, COUNTER_PORT in .env.

Typical LAN integration pattern (once protocol is known):
  - Machine runs an embedded TCP server on a configurable port (often 4000).
  - Host connects as TCP client.
  - Host sends a "start session" command frame.
  - Machine processes banknotes; sends back a result frame with denomination
    counts when the batch is complete.
  - Host sends an "acknowledge" frame; machine is ready for next session.
"""

import socket

from .base import CashCounter, CountResult
from .config import COUNTER_HOST, COUNTER_PORT

# ── Protocol constants ────────────────────────────────────────────────────────
# TODO: Replace with actual bytes from G&D documentation.
_CMD_START_SESSION: bytes = b""   # e.g. b"\x02START\x03"
_CMD_ACK: bytes = b""             # e.g. b"\x06"
_SESSION_END_MARKER: bytes = b""  # byte sequence that ends a result frame

_RECV_BUFFER: int = 4096
_CONNECT_TIMEOUT: float = 10.0

# Map G&D denomination identifiers to Nexus denomination strings.
# TODO: Update keys to match whatever denomination labels G&D sends in its
#       result frame (e.g. "EUR500", "500", "D500", …).
_DENOM_MAP: dict[str, str] = {
    "EUR500": "€500",
    "EUR200": "€200",
    "EUR100": "€100",
    "EUR050": "€50",
    "EUR020": "€20",
    "EUR010": "€10",
    "EUR005": "€5",
    "COIN":   "Coins",
}
# ─────────────────────────────────────────────────────────────────────────────


class GDC1TcpCounter(CashCounter):
    """TCP/LAN driver for the G&D BPS C1 banknote processing system."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None

    def connect(self) -> bool:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(_CONNECT_TIMEOUT)
        try:
            self._sock.connect((COUNTER_HOST, COUNTER_PORT))
            print(f"[GDC1Tcp] Connected to {COUNTER_HOST}:{COUNTER_PORT}")
            return True
        except OSError as exc:
            self._sock = None
            raise ConnectionError(
                f"Cannot reach G&D C1 at {COUNTER_HOST}:{COUNTER_PORT} — {exc}"
            ) from exc

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            print("[GDC1Tcp] Disconnected.")

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        if not self._sock:
            raise RuntimeError("Not connected. Call connect() first.")

        self._sock.settimeout(timeout_seconds)

        # TODO: Send the start-session command once the protocol is known.
        if _CMD_START_SESSION:
            self._sock.sendall(_CMD_START_SESSION)

        raw = self._receive_frame()

        # TODO: Send acknowledgment once the protocol is known.
        if _CMD_ACK:
            self._sock.sendall(_CMD_ACK)

        denominations = self._parse_result(raw)
        return CountResult(denominations=denominations, raw_response=raw)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _receive_frame(self) -> bytes:
        """Read bytes from the socket until the session-end marker is seen."""
        buf = b""
        while True:
            chunk = self._sock.recv(_RECV_BUFFER)
            if not chunk:
                break
            buf += chunk
            if _SESSION_END_MARKER and _SESSION_END_MARKER in buf:
                break
        return buf

    def _parse_result(self, raw: bytes) -> dict[str, int]:
        """
        Parse denomination counts from the machine's raw response bytes.

        TODO: Implement using the actual frame format from G&D documentation.
              The result should be a dict mapping Nexus denom strings to counts,
              e.g. {"€50": 12, "€20": 30, "€5": 5, "Coins": 0, ...}.

        For now, return zeros so the driver is importable without crashing.
        """
        print(f"[GDC1Tcp] Raw response ({len(raw)} bytes): {raw!r}")
        return {v: 0 for v in _DENOM_MAP.values()}
