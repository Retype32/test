"""
G+D BPS C1 report engine — ESC/P cleanup, stream framing, parsing and
validation.

Ported from the standalone "C1 Checker" (C1 Check.py at the project root），
a read-only capture/evidence tool that was independently proven against a
real machine. This module carries the same philosophy into the live app:

  * Frame a report by its own markers ("NO:" starts one, ESC i usually ends
    one), not by profile-driven byte-length guessing.
  * Parse with plain, fixed regexes for the one report layout the checker
    proved against real hardware -- no per-site JSON profile to keep in
    sync with the machine.
  * Never trust a report whose own printed totals disagree with its
    denomination lines. A partially-read report is the main failure mode of
    a printer-port integration; silently accepting one books a short count
    as a real one, so validation failure always raises rather than warns.
  * Never book the same physical report twice -- a report's fingerprint is
    checked against every report already read (see SeenReports) so a
    receipt reprint or a retried request can't double-count.

Fixed to EUR denominations printed as bare face values (5/10/20/50/100/200/
500), the exact set and row format ("20 X 10 = 200") the checker captured
from a real C1. Adding another currency or machine means editing this file,
not swapping a data file -- there is deliberately only one supported layout.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Face value -> Nexus denomination key (TransactionService.DENOMINATION_VALUES).
SUPPORTED_DENOMS: dict[str, str] = {
    "5": "€5", "10": "€10", "20": "€20", "50": "€50",
    "100": "€100", "200": "€200", "500": "€500",
}

# ESC i -- the C1's usual report terminator.
END_MARKER = b"\x1b\x69"


class ReportParseError(Exception):
    """A report was received but its contents could not be trusted."""


def strip_escpos(data: bytes) -> str:
    """Remove common ESC/P commands and raster payloads while preserving text."""
    result = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x1B and i + 1 < len(data):
            cmd = data[i + 1]
            if cmd == 0x2A and i + 4 < len(data):
                mode, nl, nh = data[i + 2], data[i + 3], data[i + 4]
                columns = nl + 256 * nh
                factor = 3 if mode in (32, 33) else 1
                i = min(len(data), i + 5 + columns * factor)
                continue
            if cmd in (0x61, 0x64, 0x4A) and i + 2 < len(data):
                i += 3
                continue
            if cmd in (0x40, 0x69):
                i += 2
                continue
            i += 2
            continue
        if b in (0x0A, 0x0D):
            result.append(0x0A)
        elif 32 <= b <= 126:
            result.append(b)
        elif b in (0x02, 0x03, 0x04, 0x1C):
            result.append(0x20)
        i += 1
    lines = []
    for line in result.decode("ascii", errors="replace").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def parse_decimal(value: str) -> Decimal:
    """Parse a printed amount into a Decimal, tolerant of both thousands-
    separator conventions (US: 1,234.56 / EU: 1.234,56) and of amounts with
    no separator at all -- getting this wrong must never silently corrupt a
    cash total."""
    value = value.strip().replace("EUR", "").replace("€", "").replace(" ", "")
    if not value:
        raise InvalidOperation("empty numeric value")

    last_dot = value.rfind(".")
    last_comma = value.rfind(",")
    decimal_pos = max(last_dot, last_comma)

    if decimal_pos == -1:
        return Decimal(value)

    fraction_part = value[decimal_pos + 1:]
    if fraction_part.isdigit() and len(fraction_part) in (1, 2):
        integer_part = re.sub(r"[.,]", "", value[:decimal_pos])
        cleaned = f"{integer_part}.{fraction_part}"
    else:
        cleaned = re.sub(r"[.,]", "", value)
    return Decimal(cleaned)


@dataclass
class _Row:
    denomination: Decimal
    pieces: int
    amount: Decimal


@dataclass
class ParsedReport:
    report_number: str = ""
    machine_timestamp_raw: str = ""
    user_id: str = ""
    machine_serial_number: str = ""
    currency: str = ""
    denominations: dict[str, int] = field(default_factory=dict)
    subtotal: Decimal | None = None
    coin_amount: Decimal | None = None
    total_pieces: int | None = None
    total_amount: Decimal | None = None
    reject_quantity: int | None = None
    fingerprint: str = ""


_ROW_RE = re.compile(
    r"(?<!\d)(5|10|20|50|100|200|500)(?:[.,]00)?\s*[Xx]\s*(\d+)\s*=\s*([0-9][0-9.,]*[0-9]|[0-9])"
)
_TOTAL_RE = re.compile(r"(?i)\bTotal\s+(\d+)\s*=\s*([0-9][0-9.,]*[0-9]|[0-9])")


def _fingerprint(report: ParsedReport) -> str:
    """Canonical hash identifying this exact physical report. Two reads of
    the same batch (reprint, retry, duplicate poll) produce the same
    fingerprint regardless of incidental formatting differences."""
    canonical = {
        "reportNumber": report.report_number,
        "machineTimestampRaw": report.machine_timestamp_raw,
        "machineSerialNumber": report.machine_serial_number,
        "currency": report.currency,
        "denominations": sorted(report.denominations.items()),
        "totalAmount": str(report.total_amount),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()


def parse_and_validate(text: str) -> ParsedReport:
    """Parse a cleaned report and validate its own arithmetic.

    Always strict: a report whose printed totals disagree with its own
    denomination lines raises ReportParseError rather than being accepted
    with a warning.
    """
    report = ParsedReport()

    def find(pattern: str) -> str:
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else ""

    report.report_number = find(r"\bNO\s*:\s*([0-9]+)")
    report.machine_timestamp_raw = find(r"\b(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})")
    report.user_id = find(r"\bUser ID\s*:\s*([^\n]*)")
    report.machine_serial_number = find(r"\bMachine SN\s*:\s*([^\n]*)")
    report.currency = find(r"\bCurrency\s*:?\s*([A-Z]{3})")

    rows: list[_Row] = []
    seen_keys = set()
    for m in _ROW_RE.finditer(text):
        try:
            denom_value = Decimal(m.group(1))
            pieces = int(m.group(2))
            amount = parse_decimal(m.group(3))
        except (InvalidOperation, ValueError):
            continue
        key = (denom_value, pieces, amount)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append(_Row(denom_value, pieces, amount))

    m = re.search(r"(?i)\bSubTotal\s+([0-9][0-9.,]*[0-9]|[0-9])", text)
    if m:
        report.subtotal = parse_decimal(m.group(1))
    m = re.search(r"(?i)\bCoin\s+([0-9][0-9.,]*[0-9]|[0-9])", text)
    if m:
        report.coin_amount = parse_decimal(m.group(1))
    m = _TOTAL_RE.search(text)
    if m:
        report.total_pieces = int(m.group(1))
        report.total_amount = parse_decimal(m.group(2))
    m = re.search(r"(?i)\bReject Qty\s+(\d+)", text)
    if m:
        report.reject_quantity = int(m.group(1))

    calculated_pieces = sum(r.pieces for r in rows)
    calculated_amount = sum((r.amount for r in rows), Decimal("0"))

    errors: list[str] = []
    if not report.report_number:
        errors.append("Missing report number")
    if not report.currency:
        errors.append("Missing currency")
    if not rows:
        errors.append("No denomination rows found")
    if report.total_pieces is None:
        errors.append("Missing total pieces")
    elif report.total_pieces != calculated_pieces:
        errors.append(f"Piece mismatch: rows={calculated_pieces}, total={report.total_pieces}")
    expected_total = calculated_amount + (report.coin_amount or Decimal("0"))
    if report.total_amount is None:
        errors.append("Missing total amount")
    elif report.total_amount != expected_total:
        errors.append(f"Amount mismatch: rows+coin={expected_total}, total={report.total_amount}")
    if report.subtotal is not None and report.subtotal != calculated_amount:
        errors.append(f"Subtotal mismatch: rows={calculated_amount}, subtotal={report.subtotal}")
    for row in rows:
        if row.amount != row.denomination * row.pieces:
            errors.append(f"Invalid row: {row.denomination} x {row.pieces} != {row.amount}")
        face = str(row.denomination.to_integral_value())
        if face not in SUPPORTED_DENOMS:
            errors.append(f"Unsupported denomination: {row.denomination}")

    if errors:
        raise ReportParseError(
            "Report failed its own consistency checks: " + "; ".join(errors)
            + f"\n\nRaw report received:\n{text}"
        )

    denominations: dict[str, int] = {}
    for row in rows:
        label = SUPPORTED_DENOMS[str(row.denomination.to_integral_value())]
        denominations[label] = denominations.get(label, 0) + row.pieces
    report.denominations = denominations
    report.fingerprint = _fingerprint(report)
    return report


class StreamCollector:
    """Collect reports starting at "NO:" and ending at ESC i or a qualified
    idle timeout, exactly as the standalone checker frames a live stream."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.started = False
        self.last_rx = 0.0

    def add(self, data: bytes) -> list[bytes]:
        reports: list[bytes] = []
        self.buffer.extend(data)
        self.last_rx = time.monotonic()
        if not self.started:
            marker = self.buffer.find(b"NO:")
            if marker >= 0:
                # A real report's setup sequence (reset/justify commands) sits
                # immediately before "NO:" with nothing in between -- so the
                # search window for it is deliberately short. Without a limit,
                # an unrelated ESC byte anywhere earlier in unrelated leading
                # noise could be mistaken for the start of this report's own
                # preamble and pull that noise into the framed report.
                window_start = max(0, marker - 8)
                setup = self.buffer.rfind(b"\x1b", window_start, marker)
                del self.buffer[:setup if setup >= 0 else marker]
                self.started = True
            elif len(self.buffer) > 65536:
                del self.buffer[:-1024]
        while self.started:
            end = self._find_genuine_end_marker()
            if end < 0:
                break
            reports.append(bytes(self.buffer[:end + 2]))
            del self.buffer[:end + 2]
            self.started = False
            marker = self.buffer.find(b"NO:")
            if marker >= 0:
                del self.buffer[:marker]
                self.started = True
        return reports

    def _find_genuine_end_marker(self) -> int:
        """Find ESC i, skipping any coincidental occurrence inside a raster
        (logo) block ahead of the real report text. A bare two-byte match is
        trusted only once the totals line has actually arrived ahead of it;
        otherwise the search continues past it for the genuine terminator.
        Without this, a stray ESC i in binary noise truncates the report and
        discards everything genuine that follows, including the real
        terminator -- desyncing every read after it."""
        search_from = 0
        while True:
            end = self.buffer.find(END_MARKER, search_from)
            if end < 0:
                return -1
            if b"otal" in self.buffer[:end + 2]:  # matches "Total" / "total"
                return end
            search_from = end + 1

    def idle_report(self, idle_seconds: float) -> bytes | None:
        """Emit a started-but-unterminated report once the line goes quiet.

        This is the fallback for firmware that omits ESC i, and also the
        only path by which a *malformed* report reaches the parser: the
        end-marker short-circuit above deliberately refuses to fire without
        a totals line, so without this a report that arrived garbled would
        never be framed at all and would silently time out instead of
        telling anyone it was rejected. Whatever is buffered is handed to
        the parser, which decides whether it can be trusted -- framing does
        not get to make that call.
        """
        if self.started and self.buffer and time.monotonic() - self.last_rx >= idle_seconds:
            report = bytes(self.buffer)
            self.buffer.clear()
            self.started = False
            return report
        return None

    def final_partial(self) -> bytes | None:
        if self.buffer:
            report = bytes(self.buffer)
            self.buffer.clear()
            self.started = False
            return report
        return None


class SeenReports:
    """Fingerprints of reports already booked, persisted across restarts so
    a re-delivered report (a receipt reprint, a retried request, a second
    poll landing on the same batch) is rejected instead of counted twice."""

    def __init__(self, path) -> None:
        self._path = path
        try:
            self._items: set[str] = (
                set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()
            )
        except Exception:
            self._items = set()

    def contains(self, fingerprint: str) -> bool:
        return fingerprint in self._items

    def add(self, fingerprint: str) -> None:
        import os

        self._items.add(fingerprint)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._items)), encoding="utf-8")
        os.replace(tmp, self._path)
