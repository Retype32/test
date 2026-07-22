"""
G&D BPS C1 — RS232 serial driver

The BPS C1 has an RS232 port (COM2 is reserved for the barcode reader; use
COM1 or the port assigned by your system for host communication).

The machine's serial protocol is proprietary and not publicly documented.
Obtain the specification from G&D's integration team before filling in the
TODO sections below.

Typical RS232 framing used by cash-processing machines:
  STX  <command/data bytes>  ETX  <checksum>
  (8N1, 9600 or 19200 baud — confirm with G&D documentation)

To activate this driver:
  1. Get the G&D serial protocol specification.
  2. Fill in the TODO constants and _parse_result() below.
  3. Set COUNTER_MODE=serial and COUNTER_COM_PORT / COUNTER_BAUD_RATE in .env.
"""

import serial  # pyserial

from .base import CashCounter, CountResult
from .config import COUNTER_BAUD_RATE, COUNTER_COM_PORT

# ── Protocol constants ────────────────────────────────────────────────────────
_STX: int = 0x02   # start-of-frame byte (standard; confirm with G&D docs)
_ETX: int = 0x03   # end-of-frame byte
_ACK: bytes = b"\x06"

# TODO: Replace with actual start-session command bytes from G&D documentation.
_CMD_START_SESSION: bytes = b""

# Map G&D serial denomination identifiers to Nexus denomination strings.
# TODO: Update keys to match whatever labels the machine sends in its frame.
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


class GDC1SerialCounter(CashCounter):
    """RS232 serial driver for the G&D BPS C1 banknote processing system."""

    def __init__(self) -> None:
        self._port: serial.Serial | None = None

    def connect(self) -> bool:
        try:
            self._port = serial.Serial(
                port=COUNTER_COM_PORT,
                baudrate=COUNTER_BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=10,
            )
            print(f"[GDC1Serial] Opened {COUNTER_COM_PORT} at {COUNTER_BAUD_RATE} baud.")
            return True
        except serial.SerialException as exc:
            self._port = None
            raise ConnectionError(
                f"Cannot open serial port {COUNTER_COM_PORT} — {exc}"
            ) from exc

    def disconnect(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
            print("[GDC1Serial] Port closed.")
        self._port = None

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        if not self._port or not self._port.is_open:
            raise RuntimeError("Not connected. Call connect() first.")

        self._port.timeout = timeout_seconds

        # TODO: Send start-session command once the protocol is known.
        if _CMD_START_SESSION:
            self._port.write(_CMD_START_SESSION)

        raw = self._receive_frame()

        # TODO: Send ACK once the protocol is known.
        if _ACK:
            self._port.write(_ACK)

        denominations = self._parse_result(raw)
        return CountResult(denominations=denominations, raw_response=raw)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _receive_frame(self) -> bytes:
        """Read one complete STX…ETX frame from the serial port."""
        buf = bytearray()
        in_frame = False
        while True:
            byte = self._port.read(1)
            if not byte:
                raise TimeoutError("Serial read timed out waiting for machine result.")
            val = byte[0]
            if val == _STX:
                in_frame = True
                buf.clear()
            elif val == _ETX and in_frame:
                break
            elif in_frame:
                buf.append(val)
        return bytes(buf)

    def _parse_result(self, raw: bytes) -> dict[str, int]:
        """
        Parse denomination counts from the frame payload (between STX and ETX).

        TODO: Implement using the actual frame format from G&D documentation.
              Return a dict mapping Nexus denom strings to integer counts,
              e.g. {"€50": 12, "€20": 30, "€5": 5, "Coins": 0, ...}.

        For now, return zeros so the driver is importable without crashing.
        """
        print(f"[GDC1Serial] Frame payload ({len(raw)} bytes): {raw!r}")
        return {v: 0 for v in _DENOM_MAP.values()}
