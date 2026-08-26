#!/usr/bin/env python3
"""
C1 Nexus Bridge Evidence Collector
==================================

Read-only serial capture and protocol-learning tool for G+D BPS C1 reports.

Purpose
-------
1. Monitor COM1 or COM2 at 115200, 8 data bits, no parity, 1 stop bit.
2. Preserve every complete or partial transaction as raw binary evidence.
3. Remove common ESC/P printer commands while retaining report text.
4. Parse denominations, pieces, amounts, totals and machine metadata.
5. Validate arithmetic and prevent duplicate READY transactions.
6. Produce AI-friendly JSON, JSONL, CSV and Markdown documentation.
7. Track unknown lines, byte/control sequences, port activity and report order.

The program is strictly read-only. It never writes to the serial port.

Primary output folder
---------------------
c1_nexus_data/
    raw/                       Exact binary captures
    cleaned/                   Human-readable report text
    transactions/              One complete evidence JSON per capture
    nexus_inbox/               Valid, non-duplicate parsed reports
    duplicates/                Repeated valid reports
    incomplete/                Reports that failed validation
    ai_training/
        protocol_dataset.jsonl One self-contained record per transaction
        transaction_history.jsonl
        port_events.jsonl
        unknown_lines.json
        byte_patterns.json
        dataset_manifest.json
        AI_AGENT_README.md
    report_summary.csv
    fingerprints.json
    runtime.log

Safe operating note
-------------------
Only run when operationally authorised. A Windows serial port normally permits
one owner, so this tool may be unable to open a port already held by Nexus or
another service. It does not bypass port ownership or stop any service.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import platform
import re
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

APP_VERSION = "2.0.0"
SCHEMA_VERSION = 2
DEFAULT_BAUD = 115200
SUPPORTED_DENOMS = tuple(Decimal(x) for x in ("5", "10", "20", "50", "100", "200", "500"))

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "c1_nexus_data"


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, ".2f")


def parse_decimal(value: str) -> Decimal:
    """Parse a printed amount into a Decimal, tolerant of both thousands-
    separator conventions (US: 1,234.56 / EU: 1.234,56) and of amounts with
    no separator at all. Determines the decimal point by position (whichever
    of ',' or '.' occurs LAST and is followed by exactly 1-2 digits) rather
    than assuming one fixed convention, since real report formatting is not
    yet confirmed and getting this wrong must never silently truncate a
    cash total.
    """
    value = value.strip().replace("EUR", "").replace("€", "").replace(" ", "")
    if not value:
        raise InvalidOperation("empty numeric value")

    last_dot = value.rfind(".")
    last_comma = value.rfind(",")
    decimal_pos = max(last_dot, last_comma)

    if decimal_pos == -1:
        # No separators at all - plain integer or already-clean decimal.
        return Decimal(value)

    fraction_part = value[decimal_pos + 1:]
    if fraction_part.isdigit() and len(fraction_part) in (1, 2):
        # The last separator is a genuine decimal point; every earlier
        # separator (if any) is a thousands grouping and gets dropped.
        integer_part = re.sub(r"[.,]", "", value[:decimal_pos])
        cleaned = f"{integer_part}.{fraction_part}"
    else:
        # No trailing 1-2 digit fraction (e.g. "1.234" as a bare grouped
        # integer, or a malformed value) - every separator is a thousands
        # grouping, there is no fractional part.
        cleaned = re.sub(r"[.,]", "", value)

    return Decimal(cleaned)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def printable_ascii(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b <= 126 else {10: "\\n", 13: "\\r", 9: "\\t"}.get(b, ".") for b in data)


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


@dataclass
class DenominationRow:
    denomination: Decimal
    pieces: int
    amount: Decimal

    def to_json(self) -> Dict[str, Any]:
        return {
            "denomination": decimal_text(self.denomination),
            "pieces": self.pieces,
            "amount": decimal_text(self.amount),
            "calculatedAmount": decimal_text(self.denomination * self.pieces),
            "valid": self.amount == self.denomination * self.pieces,
        }


@dataclass
class ParsedReport:
    capture_id: str
    source_port: str
    received_at: str
    received_at_utc: str
    raw_sha256: str
    raw_length: int
    report_number: str = ""
    machine_timestamp_raw: str = ""
    machine_id: str = ""
    user_id: str = ""
    machine_serial_number: str = ""
    batch_id: str = ""
    currency: str = ""
    fitness_mode: str = ""
    denominations: List[DenominationRow] = field(default_factory=list)
    subtotal: Optional[Decimal] = None
    coin_amount: Optional[Decimal] = None
    total_pieces: Optional[int] = None
    total_amount: Optional[Decimal] = None
    reject_quantity: Optional[int] = None
    cleaned_report: str = ""
    unknown_lines: List[str] = field(default_factory=list)
    raw_file: str = ""
    cleaned_file: str = ""
    validation_errors: List[str] = field(default_factory=list)
    fingerprint: str = ""
    status: str = "INCOMPLETE"
    framing_method: str = "unknown"

    @property
    def calculated_pieces(self) -> int:
        return sum(row.pieces for row in self.denominations)

    @property
    def calculated_amount(self) -> Decimal:
        return sum((row.amount for row in self.denominations), Decimal("0"))

    def validate(self) -> None:
        errors = []
        if not self.report_number:
            errors.append("Missing report number")
        if not self.currency:
            errors.append("Missing currency")
        if not self.denominations:
            errors.append("No denomination rows found")
        if self.total_pieces is None:
            errors.append("Missing total pieces")
        elif self.total_pieces != self.calculated_pieces:
            errors.append(f"Piece mismatch: rows={self.calculated_pieces}, total={self.total_pieces}")
        expected_total = self.calculated_amount + (self.coin_amount or Decimal("0"))
        if self.total_amount is None:
            errors.append("Missing total amount")
        elif self.total_amount != expected_total:
            errors.append(
                f"Amount mismatch: rows+coin={decimal_text(expected_total)}, total={decimal_text(self.total_amount)}"
            )
        if self.subtotal is not None and self.subtotal != self.calculated_amount:
            errors.append(
                f"Subtotal mismatch: rows={decimal_text(self.calculated_amount)}, subtotal={decimal_text(self.subtotal)}"
            )
        for row in self.denominations:
            if row.amount != row.denomination * row.pieces:
                errors.append(f"Invalid row: {row.denomination} x {row.pieces} != {row.amount}")
            if row.denomination not in SUPPORTED_DENOMS:
                errors.append(f"Unsupported denomination: {row.denomination}")
        self.validation_errors = errors
        self.status = "READY" if not errors else "INCOMPLETE"
        canonical = {
            "reportNumber": self.report_number,
            "machineTimestampRaw": self.machine_timestamp_raw,
            "machineSerialNumber": self.machine_serial_number,
            "currency": self.currency,
            "denominations": [r.to_json() for r in self.denominations],
            "totalPieces": self.total_pieces,
            "totalAmount": decimal_text(self.total_amount),
        }
        self.fingerprint = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def parsed_json(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "captureId": self.capture_id,
            "status": self.status,
            "source": "G+D BPS C1",
            "sourcePort": self.source_port,
            "receivedAt": self.received_at,
            "receivedAtUtc": self.received_at_utc,
            "reportNumber": self.report_number,
            "machineTimestampRaw": self.machine_timestamp_raw,
            "machineId": self.machine_id,
            "userId": self.user_id,
            "machineSerialNumber": self.machine_serial_number,
            "batchId": self.batch_id,
            "currency": self.currency,
            "fitnessMode": self.fitness_mode,
            "denominations": [r.to_json() for r in self.denominations],
            "subtotal": decimal_text(self.subtotal),
            "coinAmount": decimal_text(self.coin_amount),
            "totalPieces": self.total_pieces,
            "totalAmount": decimal_text(self.total_amount),
            "rejectQuantity": self.reject_quantity,
            "calculatedPieces": self.calculated_pieces,
            "calculatedAmount": decimal_text(self.calculated_amount),
            "validationErrors": self.validation_errors,
            "unknownLines": self.unknown_lines,
            "fingerprint": self.fingerprint,
            "rawSha256": self.raw_sha256,
            "rawLength": self.raw_length,
            "rawFile": self.raw_file,
            "cleanedFile": self.cleaned_file,
            "framingMethod": self.framing_method,
            "cleanedReport": self.cleaned_report,
        }


class ReportParser:
    # Amount groups capture any run of digits/., so a thousands-separated
    # value (e.g. "1.234,56" or "1,234.56") reaches parse_decimal intact
    # instead of being cut off at the regex level.
    row_re = re.compile(
        r"(?<!\d)(5|10|20|50|100|200|500)(?:[.,]00)?\s*[Xx]\s*(\d+)\s*=\s*([0-9][0-9.,]*[0-9]|[0-9])"
    )
    total_re = re.compile(r"(?i)\bTotal\s+(\d+)\s*=\s*([0-9][0-9.,]*[0-9]|[0-9])")
    known_line_patterns = [
        re.compile(x, re.I) for x in (
            r"^NO\s*:", r"^\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2}$",
            r"^Machine ID\s*:", r"^User ID\s*:", r"^Machine SN\s*:",
            r"^Batch ID\s*:", r"^Currency\s*:", r"^Fitness Mode\s*:",
            r"^(5|10|20|50|100|200|500)(?:[.,]00)?\s*[Xx]\s*\d+\s*=",
            r"^SubTotal\b", r"^Coin\b", r"^Total\b", r"^Reject Qty\b",
            r"^[-=_* ]+$",
        )
    ]

    @classmethod
    def parse(cls, raw: bytes, port: str, capture_id: str, framing_method: str, raw_file: str = "") -> ParsedReport:
        cleaned = strip_escpos(raw)
        report = ParsedReport(
            capture_id=capture_id,
            source_port=port,
            received_at=now_local(),
            received_at_utc=now_utc(),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_length=len(raw),
            cleaned_report=cleaned,
            raw_file=raw_file,
            framing_method=framing_method,
        )

        def find(pattern: str, flags: int = re.I) -> str:
            match = re.search(pattern, cleaned, flags)
            return match.group(1).strip() if match else ""

        report.report_number = find(r"\bNO\s*:\s*([0-9]+)")
        report.machine_timestamp_raw = find(r"\b(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})")
        report.machine_id = find(r"\bMachine ID\s*:\s*([^\n]*)")
        report.user_id = find(r"\bUser ID\s*:\s*([^\n]*)")
        report.machine_serial_number = find(r"\bMachine SN\s*:\s*([^\n]*)")
        report.batch_id = find(r"\bBatch ID\s*:\s*([^\n]*)")
        report.currency = find(r"\bCurrency\s*:\s*([A-Z]{3})")
        report.fitness_mode = find(r"\bFitness Mode\s*:\s*([^\n]*)")

        seen = set()
        for match in cls.row_re.finditer(cleaned):
            try:
                row = DenominationRow(parse_decimal(match.group(1)), int(match.group(2)), parse_decimal(match.group(3)))
            except (InvalidOperation, ValueError):
                continue
            key = (row.denomination, row.pieces, row.amount)
            if key not in seen:
                seen.add(key)
                report.denominations.append(row)

        match = re.search(r"(?i)\bSubTotal\s+([0-9][0-9.,]*[0-9]|[0-9])", cleaned)
        if match:
            report.subtotal = parse_decimal(match.group(1))
        match = re.search(r"(?i)\bCoin\s+([0-9][0-9.,]*[0-9]|[0-9])", cleaned)
        if match:
            report.coin_amount = parse_decimal(match.group(1))
        match = cls.total_re.search(cleaned)
        if match:
            report.total_pieces = int(match.group(1))
            report.total_amount = parse_decimal(match.group(2))
        match = re.search(r"(?i)\bReject Qty\s+(\d+)", cleaned)
        if match:
            report.reject_quantity = int(match.group(1))

        report.unknown_lines = [
            line for line in cleaned.splitlines()
            if line and not any(pattern.search(line) for pattern in cls.known_line_patterns)
        ]
        report.validate()
        return report


class DuplicateStore:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.items = set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()
        except Exception:
            self.items = set()

    def contains(self, fingerprint: str) -> bool:
        return fingerprint in self.items

    def add(self, fingerprint: str) -> None:
        self.items.add(fingerprint)
        atomic_write_json(self.path, sorted(self.items))


class AIKnowledgeBase:
    """Persistent evidence intended for a coding agent or protocol analyst."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.dataset = root / "protocol_dataset.jsonl"
        self.history = root / "transaction_history.jsonl"
        self.port_events = root / "port_events.jsonl"
        self.unknown_path = root / "unknown_lines.json"
        self.pattern_path = root / "byte_patterns.json"
        self.manifest_path = root / "dataset_manifest.json"
        self.readme_path = root / "AI_AGENT_README.md"
        self._lock = threading.Lock()
        self.unknown = self._load_map(self.unknown_path)
        self.patterns = self._load_map(self.pattern_path)
        self.previous_report_by_machine: Dict[str, str] = {}
        self.ensure_readme()
        self.update_manifest()

    @staticmethod
    def _load_map(path: Path) -> Dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def ensure_readme(self) -> None:
        text = f"""# C1 Protocol Evidence Dataset

Generated by C1 Nexus Bridge Evidence Collector {APP_VERSION}.

## Objective
Use these files to infer the exact G+D BPS C1 report framing, text fields,
control-byte behaviour, transaction sequence and validation rules. Treat raw
binary files as authoritative. Cleaned text and parsed JSON are interpretations.

## Important files
- `protocol_dataset.jsonl`: one complete AI-ready record per capture, including
  raw Base64, raw hex, printable representation, cleaned text and parsed fields.
- `transaction_history.jsonl`: compact chronological transaction index.
- `port_events.jsonl`: port open, close, receive, error and idle events.
- `unknown_lines.json`: unrecognised text with counts and example captures.
- `byte_patterns.json`: observed ESC/control sequences and frequencies.
- `dataset_manifest.json`: dataset counts, schema and environment information.
- `../transactions/*.json`: one expanded evidence envelope per transaction.
- `../raw/*.bin`: byte-for-byte source evidence.

## Interpretation rules
1. Never infer a protocol rule from a single capture.
2. Compare repeated captures with different denominations and totals.
3. Confirm framing using raw hex, not cleaned text.
4. `READY` means arithmetic and required fields validated.
5. `DUPLICATE` means the canonical transaction fingerprint was already stored.
6. `INCOMPLETE` means one or more required fields or arithmetic checks failed.
7. Reject quantity is recorded separately and is not included in accepted totals.
8. File paths may be absolute because evidence is intended for local analysis.

## Suggested agent workflow
1. Read `dataset_manifest.json`.
2. Read the last 100 lines of `transaction_history.jsonl`.
3. Group `protocol_dataset.jsonl` by status, port, machine serial and framing method.
4. Inspect `unknown_lines.json` from highest to lowest count.
5. Compare each parser failure with its raw `.bin` and complete evidence JSON.
6. Propose parser changes only after finding at least two supporting captures.
7. Add regression tests before changing parsing or framing logic.

## JSONL note
Each line is an independent JSON object. Process line-by-line rather than loading
the entire file into memory.
"""
        self.readme_path.write_text(text, encoding="utf-8")

    def port_event(self, port: str, event: str, **details: Any) -> None:
        append_jsonl(self.port_events, {
            "schemaVersion": SCHEMA_VERSION,
            "timestamp": now_local(),
            "timestampUtc": now_utc(),
            "port": port,
            "event": event,
            "details": details,
        })

    def observe_bytes(self, raw: bytes, capture_id: str) -> None:
        patterns = Counter()
        for i, value in enumerate(raw):
            if value == 0x1B and i + 1 < len(raw):
                patterns[f"ESC {raw[i + 1]:02X}"] += 1
            elif value < 0x20 and value not in (0x09, 0x0A, 0x0D):
                patterns[f"CTRL {value:02X}"] += 1
        for key, count in patterns.items():
            entry = self.patterns.setdefault(key, {"count": 0, "firstSeen": now_local(), "lastSeen": "", "examples": []})
            entry["count"] += count
            entry["lastSeen"] = now_local()
            if capture_id not in entry["examples"] and len(entry["examples"]) < 10:
                entry["examples"].append(capture_id)
        atomic_write_json(self.pattern_path, self.patterns)

    def record(self, report: ParsedReport, raw: bytes, evidence_file: str) -> None:
        with self._lock:
            for line in report.unknown_lines:
                normalized = re.sub(r"\s+", " ", line).strip()
                entry = self.unknown.setdefault(normalized, {
                    "count": 0, "firstSeen": report.received_at,
                    "lastSeen": "", "exampleCaptureIds": []
                })
                entry["count"] += 1
                entry["lastSeen"] = report.received_at
                if report.capture_id not in entry["exampleCaptureIds"] and len(entry["exampleCaptureIds"]) < 10:
                    entry["exampleCaptureIds"].append(report.capture_id)
            atomic_write_json(self.unknown_path, self.unknown)
            self.observe_bytes(raw, report.capture_id)

            machine_key = report.machine_serial_number or report.machine_id or "UNKNOWN"
            previous = self.previous_report_by_machine.get(machine_key)
            self.previous_report_by_machine[machine_key] = report.report_number

            parsed = report.parsed_json()
            dataset_record = {
                "schemaVersion": SCHEMA_VERSION,
                "captureId": report.capture_id,
                "evidenceFile": evidence_file,
                "raw": {
                    "length": len(raw),
                    "sha256": report.raw_sha256,
                    "hex": raw.hex(" "),
                    "base64": base64.b64encode(raw).decode("ascii"),
                    "printableAscii": printable_ascii(raw),
                },
                "cleanedText": report.cleaned_report,
                "parsed": parsed,
                "analysisHints": {
                    "containsEsc": b"\x1b" in raw,
                    "containsStartMarkerNO": b"NO:" in raw,
                    "containsCutMarkerESC_i": b"\x1b\x69" in raw,
                    "containsTotalText": b"Total" in raw,
                    "containsRejectQtyText": b"Reject Qty" in raw,
                    "unknownLineCount": len(report.unknown_lines),
                    "parserPassed": report.status in ("READY", "DUPLICATE"),
                },
            }
            append_jsonl(self.dataset, dataset_record)
            append_jsonl(self.history, {
                "schemaVersion": SCHEMA_VERSION,
                "captureId": report.capture_id,
                "receivedAt": report.received_at,
                "port": report.source_port,
                "machineSerialNumber": report.machine_serial_number,
                "reportNumber": report.report_number,
                "previousReportNumberSeenThisRun": previous,
                "totalPieces": report.total_pieces,
                "totalAmount": decimal_text(report.total_amount),
                "currency": report.currency,
                "status": report.status,
                "framingMethod": report.framing_method,
                "fingerprint": report.fingerprint,
                "rawSha256": report.raw_sha256,
                "evidenceFile": evidence_file,
            })
            self.update_manifest()

    def update_manifest(self) -> None:
        def line_count(path: Path) -> int:
            if not path.exists():
                return 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for line in fh if line.strip())

        atomic_write_json(self.manifest_path, {
            "schemaVersion": SCHEMA_VERSION,
            "application": "C1 Nexus Bridge Evidence Collector",
            "applicationVersion": APP_VERSION,
            "updatedAt": now_local(),
            "datasetRecords": line_count(self.dataset),
            "historyRecords": line_count(self.history),
            "portEvents": line_count(self.port_events),
            "distinctUnknownLines": len(self.unknown),
            "distinctBytePatterns": len(self.patterns),
            "serialDefaults": {"baud": DEFAULT_BAUD, "dataBits": 8, "parity": "N", "stopBits": 1, "handshake": "none"},
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "executable": sys.executable,
                "frozenExe": bool(getattr(sys, "frozen", False)),
            },
        })


class ResultWriter:
    def __init__(self, root: Path):
        self.root = root
        self.raw_dir = root / "raw"
        self.clean_dir = root / "cleaned"
        self.transaction_dir = root / "transactions"
        self.inbox_dir = root / "nexus_inbox"
        self.duplicate_dir = root / "duplicates"
        self.incomplete_dir = root / "incomplete"
        for folder in (self.raw_dir, self.clean_dir, self.transaction_dir, self.inbox_dir, self.duplicate_dir, self.incomplete_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.duplicates = DuplicateStore(root / "fingerprints.json")
        self.ai = AIKnowledgeBase(root / "ai_training")
        self.csv_path = root / "report_summary.csv"
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerow([
                    "Capture ID", "Received At", "Port", "Report Number", "Machine Time",
                    "Machine SN", "Currency", "Total Pieces", "Total Amount", "Reject Qty",
                    "Status", "Framing", "Raw Bytes", "Unknown Lines", "Fingerprint"
                ])

    def capture_id(self, port: str, raw: bytes) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"{port}_{stamp}_{hashlib.sha256(raw).hexdigest()[:10]}"

    def save_raw(self, raw: bytes, capture_id: str) -> Path:
        path = self.raw_dir / f"{capture_id}.bin"
        path.write_bytes(raw)
        return path

    def save_report(self, report: ParsedReport, raw: bytes) -> Tuple[Path, Path]:
        clean_path = self.clean_dir / f"{report.capture_id}.txt"
        clean_path.write_text(report.cleaned_report, encoding="utf-8")
        report.cleaned_file = str(clean_path)

        if report.status == "READY" and self.duplicates.contains(report.fingerprint):
            report.status = "DUPLICATE"
            destination = self.duplicate_dir
        elif report.status == "READY":
            destination = self.inbox_dir
            self.duplicates.add(report.fingerprint)
        else:
            destination = self.incomplete_dir

        parsed_path = destination / f"{report.capture_id}.json"
        atomic_write_json(parsed_path, report.parsed_json())

        evidence = {
            "schemaVersion": SCHEMA_VERSION,
            "captureId": report.capture_id,
            "purpose": "Self-contained C1 transaction evidence for human and AI analysis",
            "raw": {
                "file": report.raw_file,
                "length": len(raw),
                "sha256": report.raw_sha256,
                "hex": raw.hex(" "),
                "base64": base64.b64encode(raw).decode("ascii"),
                "printableAscii": printable_ascii(raw),
            },
            "transformation": {
                "cleanedFile": str(clean_path),
                "cleanedText": report.cleaned_report,
                "method": "Common ESC/P command and raster removal; original raw bytes retained",
            },
            "parsed": report.parsed_json(),
            "resultFile": str(parsed_path),
        }
        evidence_path = self.transaction_dir / f"{report.capture_id}.json"
        atomic_write_json(evidence_path, evidence)
        self.ai.record(report, raw, str(evidence_path))

        with self.csv_path.open("a", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow([
                report.capture_id, report.received_at, report.source_port, report.report_number,
                report.machine_timestamp_raw, report.machine_serial_number, report.currency,
                report.total_pieces, decimal_text(report.total_amount), report.reject_quantity,
                report.status, report.framing_method, report.raw_length,
                len(report.unknown_lines), report.fingerprint,
            ])
        return parsed_path, evidence_path

    def process(self, raw: bytes, port: str, framing_method: str) -> Tuple[ParsedReport, Path, Path]:
        capture_id = self.capture_id(port, raw)
        raw_path = self.save_raw(raw, capture_id)
        report = ReportParser.parse(raw, port, capture_id, framing_method, str(raw_path))
        parsed_path, evidence_path = self.save_report(report, raw)
        return report, parsed_path, evidence_path


class StreamCollector:
    """Collect reports starting at NO: and ending at ESC i or a qualified idle."""

    def __init__(self):
        self.buffer = bytearray()
        self.started = False
        self.last_rx = 0.0

    def add(self, data: bytes) -> List[Tuple[bytes, str]]:
        reports = []
        self.buffer.extend(data)
        self.last_rx = time.monotonic()
        if not self.started:
            marker = self.buffer.find(b"NO:")
            if marker >= 0:
                setup = self.buffer.rfind(b"\x1b", 0, marker)
                del self.buffer[:setup if setup >= 0 else marker]
                self.started = True
            elif len(self.buffer) > 65536:
                del self.buffer[:-1024]
        while self.started:
            end = self.buffer.find(b"\x1b\x69")
            if end < 0:
                break
            reports.append((bytes(self.buffer[:end + 2]), "ESC_i"))
            del self.buffer[:end + 2]
            self.started = False
            marker = self.buffer.find(b"NO:")
            if marker >= 0:
                del self.buffer[:marker]
                self.started = True
        return reports

    def idle_report(self, idle_seconds: float) -> Optional[Tuple[bytes, str]]:
        if self.started and self.buffer and time.monotonic() - self.last_rx >= idle_seconds:
            cleaned = strip_escpos(bytes(self.buffer))
            if "Total" in cleaned or "Reject Qty" in cleaned:
                report = bytes(self.buffer)
                self.buffer.clear()
                self.started = False
                return report, "qualified_idle"
        return None

    def final_partial(self) -> Optional[Tuple[bytes, str]]:
        if self.buffer:
            report = bytes(self.buffer)
            self.buffer.clear()
            self.started = False
            return report, "shutdown_partial"
        return None


def configure_logging(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(root / "runtime.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def print_report(report: ParsedReport, parsed_path: Path, evidence_path: Path) -> None:
    print("\n" + "=" * 72)
    print(f"C1 REPORT {report.report_number or 'UNKNOWN'} | {report.status} | {report.capture_id}")
    print("=" * 72)
    print(f"Port:       {report.source_port}")
    print(f"Machine:    {report.machine_serial_number or '-'}")
    print(f"C1 time:    {report.machine_timestamp_raw or '-'}")
    print(f"User:       {report.user_id or '-'}")
    print(f"Currency:   {report.currency or '-'}")
    print(f"Framing:    {report.framing_method}")
    print("-" * 72)
    print(f"{'Denomination':>14} {'Pieces':>10} {'Amount':>16} {'Valid':>8}")
    for row in report.denominations:
        print(f"{decimal_text(row.denomination):>14} {row.pieces:>10} {decimal_text(row.amount):>16} {str(row.amount == row.denomination * row.pieces):>8}")
    if not report.denominations:
        print("No denomination rows recognised")
    print("-" * 72)
    print(f"Accepted pieces: {report.total_pieces if report.total_pieces is not None else '-'}")
    print(f"Accepted amount: {decimal_text(report.total_amount) or '-'}")
    print(f"Reject quantity: {report.reject_quantity if report.reject_quantity is not None else '-'}")
    print(f"Unknown lines:   {len(report.unknown_lines)}")
    print(f"Validation:      {'PASS' if not report.validation_errors else 'FAIL'}")
    for error in report.validation_errors:
        print("  ! " + error)
    print(f"Parsed file:     {parsed_path}")
    print(f"Evidence file:   {evidence_path}")
    print("=" * 72 + "\n")


def open_serial(port: str, baud: int):
    connection = serial.Serial(
        port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
        timeout=0.10, write_timeout=0.5, xonxoff=False,
        rtscts=False, dsrdtr=False,
    )
    connection.rts = False
    connection.dtr = False
    return connection


def run_monitor(port: str, baud: int, idle_seconds: float) -> int:
    if serial is None:
        print("ERROR: pyserial is missing. Run: python -m pip install pyserial")
        return 2
    configure_logging(DATA_DIR)
    writer = ResultWriter(DATA_DIR)
    collector = StreamCollector()
    stop = threading.Event()

    def request_stop(signum=None, frame=None):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    print("=" * 76)
    print("C1 NEXUS BRIDGE EVIDENCE COLLECTOR")
    print(f"Listening read-only on {port} at {baud} 8N1, no handshake")
    print(f"Output: {DATA_DIR}")
    print("Press Ctrl+C to stop.")
    print("=" * 76)

    try:
        connection = open_serial(port, baud)
        writer.ai.port_event(port, "PORT_OPENED", baud=baud, dataBits=8, parity="N", stopBits=1)
    except Exception as exc:
        writer.ai.port_event(port, "PORT_OPEN_FAILED", error=repr(exc), baud=baud)
        print(f"ERROR opening {port}: {exc}")
        print("The port may not exist or may already be owned by another process (e.g. Nexus).")
        return 1

    total_bytes = 0
    try:
        while not stop.is_set():
            data = connection.read(connection.in_waiting or 1)
            if data:
                total_bytes += len(data)
                writer.ai.port_event(port, "BYTES_RECEIVED", byteCount=len(data), cumulativeBytes=total_bytes, sampleHex=data[:32].hex(" "))
                for raw, framing in collector.add(data):
                    report, parsed, evidence = writer.process(raw, port, framing)
                    print_report(report, parsed, evidence)
            else:
                item = collector.idle_report(idle_seconds)
                if item:
                    raw, framing = item
                    report, parsed, evidence = writer.process(raw, port, framing)
                    print_report(report, parsed, evidence)
    except Exception as exc:
        writer.ai.port_event(port, "MONITOR_ERROR", error=repr(exc), cumulativeBytes=total_bytes)
        logging.exception("Serial monitoring failed")
        return 1
    finally:
        partial = collector.final_partial()
        if partial:
            raw, framing = partial
            report, parsed, evidence = writer.process(raw, port, framing)
            print_report(report, parsed, evidence)
        try:
            connection.close()
        except Exception:
            pass
        writer.ai.port_event(port, "PORT_CLOSED", cumulativeBytes=total_bytes)
        print(f"Port closed. Results folder: {DATA_DIR}")
    return 0


def selftest() -> int:
    configure_logging(DATA_DIR)
    print("Running parser, validation, duplicate, evidence and AI dataset self-test...")
    raster = b"\x1b\x2a\x21\x02\x00" + (b"R" * 6)
    report_a = (
        b"\x1b@\x1ba\x02NO:00001\n"
        b"\x1ba\x0008-06-2026 15:32:47\n"
        b"\x1ba\x00Machine ID : --------\n\x1ba\x00User ID : User1\n"
        b"\x1ba\x00Machine SN : 315190\n\x1ba\x00Batch ID : B100\n"
        b"\x1ba\x00Currency: EUR\n\x1ba\x00Fitness Mode : None\n"
        + raster + b"10 X 1 = \x04 10\n"
        b"\x1ba\x00SubTotal 10\n\x1ba\x00Coin 0.00\n"
        b"\x1ba\x00Total 1 = 10\n\x1ba\x00Reject Qty 2\n"
        b"\x1ba\x00Unexpected Field : SAMPLE\n\x1bd\x04\n\x1bi"
    )
    report_b = (
        b"\x1b@\x1ba\x02NO:00002\n"
        b"\x1ba\x0008-06-2026 15:33:28\n\x1ba\x00User ID : User1\n"
        b"\x1ba\x00Machine SN : 315190\n\x1ba\x00Currency: EUR\n"
        + raster + b"20 X 10 = \x04 200\n"
        + raster + b"10 X 15 = \x04 150\n"
        + raster + b"5 X 28 = \x04 140\n"
        b"\x1ba\x00SubTotal 490\n\x1ba\x00Coin 0.00\n"
        b"\x1ba\x00Total 53 = 490\n\x1ba\x00Reject Qty 2\n\x1bd\x04\n\x1bi"
    )
    test_root = DATA_DIR / "selftest"
    if test_root.exists():
        import shutil
        shutil.rmtree(test_root)
    writer = ResultWriter(test_root)
    failures = []

    r1, p1, e1 = writer.process(report_a, "TEST", "selftest")
    if r1.status != "READY" or r1.total_amount != Decimal("10") or r1.total_pieces != 1:
        failures.append("Report 1 parsing/validation failed")
    if "Unexpected Field : SAMPLE" not in r1.unknown_lines:
        failures.append("Unknown-line learning failed")

    r2, p2, e2 = writer.process(report_b, "TEST", "selftest")
    if r2.status != "READY" or r2.total_amount != Decimal("490") or r2.total_pieces != 53:
        failures.append("Report 2 parsing/validation failed")

    rdup, _, _ = writer.process(report_b, "TEST", "selftest_duplicate")
    if rdup.status != "DUPLICATE":
        failures.append("Duplicate detection failed")

    incomplete = b"NO:00003\nCurrency: EUR\n20 X 2 = 40\n"
    rinc, _, _ = writer.process(incomplete, "TEST", "selftest_partial")
    if rinc.status != "INCOMPLETE":
        failures.append("Incomplete-report protection failed")

    required = [
        writer.ai.dataset, writer.ai.history, writer.ai.unknown_path,
        writer.ai.pattern_path, writer.ai.manifest_path, writer.ai.readme_path,
        e1, e2, p1, p2,
    ]
    for path in required:
        if not path.exists() or path.stat().st_size == 0:
            failures.append(f"Missing or empty output: {path}")

    print_report(r1, p1, e1)
    print_report(r2, p2, e2)
    print(f"Duplicate test:  {rdup.status}")
    print(f"Incomplete test: {rinc.status}")
    if failures:
        print("SELFTEST FAILED")
        for item in failures:
            print(" - " + item)
        return 1
    print("SELFTEST PASS")
    print(f"Test evidence: {test_root}")
    return 0


def list_serial_ports() -> int:
    if list_ports is None:
        print("pyserial is not installed.")
        return 2
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
    for item in ports:
        print(f"{item.device}: {item.description} [{item.hwid}]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only C1 report capture and AI evidence collector")
    parser.add_argument("--port", default="COM1", help="Serial port, normally COM1 or COM2")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--list-ports", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.list_ports:
        return list_serial_ports()
    return run_monitor(args.port.upper(), args.baud, args.idle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
