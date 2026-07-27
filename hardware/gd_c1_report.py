"""
G+D BPS C1 — serial report driver.

The C1 has no host command API. It emits a batch report to its serial printer
interface when the operator ends a batch, and the host reads that report. This
driver therefore only listens: it never sends a command to the machine.

Framing is by idle time. The machine streams the whole report in one burst;
once the line has been quiet for `idle_timeout_seconds` the report is complete.

Machine-side setup:
  * Configure the C1's report/printer output to the serial interface using the
    line settings in the device profile (115200 8N1, no handshake by default).
  * Use COM1 or another free port — COM2 on the C1 is the barcode reader.
  * Connect with a null-modem cable to the host PC.

Host-side setup (.env):
  COUNTER_MODE=c1_report
  COUNTER_COM_PORT=COM3
  COUNTER_PROFILE=bps_c1_eur
"""

from __future__ import annotations

import time

import serial  # pyserial

from .base import CashCounter, CountResult
from .config import (
    COUNTER_AUTODETECT,
    COUNTER_BAUD_RATE,
    COUNTER_COM_PORT,
    COUNTER_PROFILE,
    COUNTER_STRICT,
)
from .report_parser import ReportParseError, detect_profile, parse_report
from .report_profile import DeviceProfile, load_profile

_PARITY = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
_STOPBITS = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}
_BYTESIZE = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}


def _open_failure_message(port_name: str, exc: Exception) -> str:
    """Explain a failed port open in terms of what actually goes wrong on site.

    Windows reports a port that another program already owns as a bare
    "Access is denied", which gives no hint that ISA is usually the owner.
    """
    detail = str(exc)
    lowered = detail.lower()

    if "access is denied" in lowered or "permission" in lowered:
        return (
            f"Serial port {port_name} is already in use by another program.\n"
            "A serial port has exactly one owner. If ISA is running on this PC "
            "it holds the machine's printer port open for as long as its BPS C1 "
            "plugin is loaded, which locks Nexus out.\n"
            "Options: close ISA, point Nexus at a different port on the machine "
            "(not COM2 — that is the barcode reader), run Nexus on a separate "
            "PC, or split the line with a passive Y-cable so both hosts receive "
            "the same printout.\n"
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
                rtscts=comms.rtscts,
                dsrdtr=comms.dsrdtr,
                timeout=0.2,
            )
        except serial.SerialException as exc:
            self._port = None
            raise ConnectionError(_open_failure_message(port_name, exc)) from exc

        self._port.reset_input_buffer()
        print(f"[C1Report] Listening on {port_name} at {baud} baud ({self._profile.name}).")
        return True

    def disconnect(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
            print("[C1Report] Port closed.")
        self._port = None

    def wait_for_count_result(self, timeout_seconds: int = 120) -> CountResult:
        raw = self.read_raw_report(timeout_seconds)
        text = raw.decode(self._profile.encoding, errors="replace")

        report = self._parse(text)

        if report.warnings:
            for warning in report.warnings:
                print(f"[C1Report] WARNING: {warning}")
            if COUNTER_STRICT:
                raise ReportParseError(
                    "Report failed its own consistency checks: "
                    + "; ".join(report.warnings)
                    + f"\n\nRaw report received:\n{text}"
                )
        if report.unmatched_lines:
            print(f"[C1Report] Unrecognised lines ignored: {report.unmatched_lines}")

        return CountResult(denominations=report.denominations, raw_response=raw)

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

            print(
                f"[C1Report] Profile {self._profile.name!r} did not match this "
                f"report; {profile.name!r} did. Set COUNTER_PROFILE accordingly."
            )
            return report

    def read_raw_report(self, timeout_seconds: int = 120) -> bytes:
        """Block until a full report burst has been received.

        Returns once the machine has sent at least `min_report_bytes` and then
        gone quiet for `idle_timeout_seconds`.
        """
        if not self._port or not self._port.is_open:
            raise RuntimeError("Not connected. Call connect() first.")

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
                    print(
                        f"[C1Report] Timeout with {len(buf)} bytes buffered; "
                        "parsing what arrived."
                    )
                    return bytes(buf)
                noise = f" Received {len(buf)} stray byte(s)." if buf else ""
                raise TimeoutError(
                    f"No report received within {timeout_seconds}s.{noise} Check that "
                    "the C1 report output is routed to the serial port and that the "
                    "cable and line settings match the device profile."
                )
