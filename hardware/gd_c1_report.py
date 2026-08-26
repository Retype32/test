"""
G+D BPS C1 — serial report driver.

The C1 has no host command API. It emits a batch report to its serial printer
interface when the operator ends a batch, and the host reads that report. This
driver therefore only listens: it never sends a command to the machine.

Framing is by idle time. The machine streams the whole report in one burst;
once the line has been quiet for `idle_timeout_seconds` the report is complete.

Machine-side setup:
  * Configure the C1's report/printer output to a serial interface using the
    line settings in the device profile (115200 8N1, no handshake by default).
  * Connect that interface to the host PC with a null-modem cable.

Host-side setup (.env). COUNTER_COM_PORT is the port name on the PC, which has
nothing to do with which physical socket is used on the machine:

  COUNTER_MODE=c1_report
  COUNTER_COM_PORT=COM3
  COUNTER_PROFILE=bps_c1_eur
"""

from __future__ import annotations

import logging
import time
from decimal import ROUND_HALF_UP

import serial  # pyserial

from .base import CashCounter, CountResult
from .config import (
    COUNTER_AUTODETECT,
    COUNTER_BAUD_RATE,
    COUNTER_COM_PORT,
    COUNTER_PROFILE,
    COUNTER_STRICT,
)
from .escpos import strip_escpos
from .report_parser import ReportParseError, detect_profile, parse_report
from .report_profile import DeviceProfile, load_profile

logger = logging.getLogger(__name__)

# ESC i -- the C1's usual report terminator. Finding it lets a report be
# accepted immediately instead of always waiting out the idle timeout; the
# idle timeout remains the fallback for firmware that omits it.
_END_MARKER = b"\x1b\x69"

_PARITY = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
_STOPBITS = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}
_BYTESIZE = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}


def _open_failure_message(port_name: str, exc: Exception) -> str:
    """Explain a failed port open in terms of what actually goes wrong on site.

    Windows reports a port that another program already owns as a bare
    "Access is denied", which gives no hint who the owner actually is.
    """
    detail = str(exc)
    lowered = detail.lower()

    if "access is denied" in lowered or "permission" in lowered:
        return (
            f"Serial port {port_name} is already in use by another program.\n"
            "A serial port has exactly one owner, and Nexus needs to be it. "
            "Close whatever else has it open -- another Nexus instance still "
            "running, a diagnostic tool such as C1 Check.py or "
            "hardware.capture left connected, or any other program on this "
            "PC using this port -- then restart Nexus so it can claim it.\n"
            f"Original error: {detail}"
        )

    if "could not open port" in lowered or "filenotfound" in lowered:
        return (
            f"Serial port {port_name} does not exist on this PC.\n"
            "Run 'python -m hardware.capture --list-ports' to see the available "
            "ports, then set COUNTER_COM_PORT in .env to the right one.\n"
            f"Original error: {detail}"
        )

    return f"Cannot open serial port {port_name} — {detail}"


class GDC1ReportCounter(CashCounter):
    """Reads BPS C1 batch reports from the machine's serial printer port."""

    def __init__(self, profile: DeviceProfile | None = None) -> None:
        self._profile = profile or load_profile(COUNTER_PROFILE)
        self._port: serial.Serial | None = None

    @property
    def profile(self) -> DeviceProfile:
        return self._profile

    def connect(self) -> bool:
        comms = self._profile.comms
        port_name = COUNTER_COM_PORT or comms.port
        baud = COUNTER_BAUD_RATE or comms.baudrate
        try:
            self._port = serial.Serial(
                port=port_name,
                baudrate=baud,
                bytesize=_BYTESIZE[comms.bytesize],
                parity=_PARITY[comms.parity.upper()],
                stopbits=_STOPBITS[comms.stopbits],
                xonxoff=False,
                rtscts=comms.rtscts,
                dsrdtr=comms.dsrdtr,
                timeout=0.10,
                write_timeout=0.5,
            )
        except serial.SerialException as exc:
            self._port = None
            raise ConnectionError(_open_failure_message(port_name, exc)) from exc

        # Some C1 units hold their report output back while RTS/DTR read
        # asserted, since a plain listener never raises them itself. This is
        # a read-only driver -- it never writes a command byte -- so forcing
        # both low costs nothing and matches the wiring proven against a
        # real machine.
        self._port.rts = False
        self._port.dtr = False
        self._port.reset_input_buffer()
        logger.info("Listening on %s at %s baud (%s).", port_name, baud, self._profile.name)
        return True

    def disconnect(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
            logger.info("Port closed.")
        self._port = None

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        raw = self.read_raw_report(timeout_seconds)
        # Byte-level cleanup first: raster/ESC-P blocks are binary and can
        # only be skipped by the length their command header declares, which
        # is lost once the stream has already been decoded as text.
        text = strip_escpos(raw)

        report = self._parse(text)

        if report.warnings:
            for warning in report.warnings:
                logger.warning(warning)
            if COUNTER_STRICT:
                raise ReportParseError(
                    "Report failed its own consistency checks: "
                    + "; ".join(report.warnings)
                    + f"\n\nRaw report received:\n{text}"
                )
        if report.unmatched_lines:
            logger.info("Unrecognised lines ignored: %s", report.unmatched_lines)
        if report.report_number:
            logger.info(
                "Report %s (user %s, machine SN %s)",
                report.report_number, report.user_id or "?", report.machine_serial_number or "?",
            )

        denominations = dict(report.denominations)
        if report.coin_amount:
            # The wizard's "Coins" field is a bulk euro amount, not a piece
            # count (TransactionService.DENOMINATION_VALUES["Coins"] == 1),
            # so the coin total maps onto it directly.
            denominations["Coins"] = int(
                report.coin_amount.to_integral_value(rounding=ROUND_HALF_UP)
            )

        return CountResult(denominations=denominations, raw_response=raw)

    # ── internals ─────────────────────────────────────────────────────────────

    def _parse(self, text: str):
        """Parse with the configured profile, falling back to detection.

        A machine whose print template differs from the configured profile is
        the most likely first-run problem on a new site. Rather than fail
        outright, try the other installed profiles — but only accept one whose
        printed totals agree with its own denomination lines, and say loudly
        which profile was used so the .env can be corrected.
        """
        try:
            return parse_report(text, self._profile)
        except ReportParseError as exc:
            if not COUNTER_AUTODETECT:
                raise ReportParseError(f"{exc}\n\nRaw report received:\n{text}") from exc

            try:
                profile, report = detect_profile(text)
            except ReportParseError:
                raise ReportParseError(
                    f"{exc}\n\nNo other installed profile matched either.\n"
                    f"Raw report received:\n{text}"
                ) from exc

            if report.warnings:
                raise ReportParseError(
                    f"{exc}\n\nProfile {profile.name!r} came closest but its "
                    f"totals disagree: {'; '.join(report.warnings)}\n"
                    f"Raw report received:\n{text}"
                ) from exc

            logger.warning(
                "Profile %r did not match this report; %r did. Set COUNTER_PROFILE accordingly.",
                self._profile.name, profile.name,
            )
            return report

    def _looks_like_a_complete_report(self, buf: bytearray) -> bool:
        """Guard the end-marker short-circuit against a false match.

        ESC i is an ordinary two-byte sequence, and the C1's report burst
        includes raster (bitmap) blocks for a logo ahead of the actual text --
        arbitrary binary data that can coincidentally contain those same two
        bytes before the real report has finished arriving. Trusting the
        first occurrence blindly would return a truncated buffer, leaving the
        rest of the transmission (including the genuine terminator) unread
        and desyncing every read after it. Requiring the totals line to
        already be present is a cheap way to tell a real end marker from
        noise without having to parse the raster block's own length header.
        """
        totals_label = self._profile.label("totals")
        if not totals_label:
            return True
        return totals_label in strip_escpos(bytes(buf))

    def read_raw_report(self, timeout_seconds: int = 120) -> bytes:
        """Block until a full report burst has been received.

        Returns once the machine has sent at least `min_report_bytes` and then
        gone quiet for `idle_timeout_seconds`.
        """
        if not self._port or not self._port.is_open:
            raise RuntimeError("Not connected. Call connect() first.")

        # Each call is meant to capture exactly one fresh batch. Drop anything
        # still sitting in the port's receive buffer from before this call --
        # trailing bytes the previous report's burst left behind (e.g. a cut
        # command after its own end marker) would otherwise prefix this read
        # and desync every batch from here on, since nothing below re-aligns
        # to a report boundary once the byte stream has drifted.
        self._port.reset_input_buffer()

        profile = self._profile
        buf = bytearray()
        deadline = time.monotonic() + timeout_seconds
        last_byte_at: float | None = None

        while True:
            chunk = self._port.read(4096)
            now = time.monotonic()

            if chunk:
                buf.extend(chunk)
                last_byte_at = now
                if (
                    len(buf) >= profile.min_report_bytes
                    and _END_MARKER in buf
                    and self._looks_like_a_complete_report(buf)
                ):
                    return bytes(buf)
            elif (
                last_byte_at is not None
                and len(buf) >= profile.min_report_bytes
                and now - last_byte_at >= profile.idle_timeout_seconds
            ):
                return bytes(buf)

            if now >= deadline:
                # Anything shorter than a plausible report is line noise, not a
                # truncated count — report it as silence rather than letting the
                # parser fail on it, which would hide the real problem.
                if len(buf) >= profile.min_report_bytes:
                    logger.warning("Timeout with %d bytes buffered; parsing what arrived.", len(buf))
                    return bytes(buf)
                noise = f" Received {len(buf)} stray byte(s)." if buf else ""
                raise TimeoutError(
                    f"No report received within {timeout_seconds}s.{noise} Check that "
                    "the C1 report output is routed to the serial port and that the "
                    "cable and line settings match the device profile."
                )
