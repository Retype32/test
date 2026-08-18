"""
Parser for the end-of-batch report a cash counter prints to its serial port.

The BPS C1 (and most machines in this class) has no host API. It drives a
serial receipt printer, and integration works by connecting the host to that
printer port and reading the report as text. This module turns that text into
denomination counts.

The parser is deliberately tolerant: column widths, spacing and wording vary
between firmware versions and print templates, so lines are classified by the
labels in the device profile and by which tokens are numeric, never by fixed
column offsets.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .report_profile import DeviceProfile, load_all_profiles

_NUMERIC = re.compile(r"^[+-]?\d[\d., ']*$")
_CONTROL = {"\x00", "\x0c", "\x1b", "\x07", "\x0e", "\x0f"}


@dataclass
class ParsedReport:
    denominations: dict[str, int] = field(default_factory=dict)
    values: dict[str, Decimal] = field(default_factory=dict)
    currency: str | None = None
    fitness_mode: str | None = None
    coin_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    total_quantity: int | None = None
    total_value: Decimal | None = None
    subtotal_quantity: int | None = None
    subtotal_value: Decimal | None = None
    reject_quantity: int = 0
    report_number: str | None = None
    user_id: str | None = None
    machine_serial_number: str | None = None
    batch_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    unmatched_lines: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def counted_quantity(self) -> int:
        return sum(self.denominations.values())

    @property
    def counted_note_value(self) -> Decimal:
        """Sum of the denomination rows only, excluding bulk coin."""
        return sum(self.values.values(), Decimal(0))

    @property
    def counted_value(self) -> Decimal:
        """Denomination rows plus bulk coin -- what the machine's Total covers."""
        return self.counted_note_value + self.coin_amount

    @property
    def is_reliable(self) -> bool:
        """True when the report parsed cleanly and its own totals agree."""
        return bool(self.denominations) and not self.warnings


class ReportParseError(ValueError):
    """The text received could not be interpreted as a count report."""


def parse_report(text: str, profile: DeviceProfile) -> ParsedReport:
    report = ParsedReport(raw_text=text)
    lines = _clean_lines(text, profile)
    if not lines:
        raise ReportParseError("Report contained no printable lines.")

    for line in lines:
        _classify_line(line, profile, report)

    if not report.denominations:
        raise ReportParseError(
            "No denomination lines recognised. The print template or the "
            "denomination names in the device profile likely need adjusting. "
            f"Lines seen: {lines[:12]}"
        )

    _validate_totals(report)
    return report


# ── line handling ────────────────────────────────────────────────────────────


def _clean_lines(text: str, profile: DeviceProfile) -> list[str]:
    """Strip printer control characters, separators and blank lines."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = "".join(ch for ch in raw if ch not in _CONTROL)
        line = "".join(ch for ch in line if unicodedata.category(ch) != "Cc" or ch == "\t")
        line = line.replace("\t", " ").replace(" ", " ").strip()
        if not line:
            continue
        if profile.separator and set(line) <= set(profile.separator + " "):
            continue
        out.append(line)
    return out


_METADATA_LABELS = (
    ("report_number", "report_number"),
    ("user_id", "user_id"),
    ("machine_serial", "machine_serial_number"),
    ("batch_id", "batch_id"),
)


def _classify_line(line: str, profile: DeviceProfile, report: ParsedReport) -> None:
    lower = line.lower()

    if _is_label_line(lower, profile.label("currency")) and report.currency is None:
        report.currency = _label_value(line, profile.label("currency"))
        return

    if _is_label_line(lower, profile.label("fitness")) and report.fitness_mode is None:
        report.fitness_mode = _label_value(line, profile.label("fitness"))
        return

    for label_key, attr in _METADATA_LABELS:
        label = profile.label(label_key)
        if label and _is_label_line(lower, label) and getattr(report, attr) is None:
            setattr(report, attr, _label_value(line, label))
            return

    if _is_label_line(lower, profile.label("coin")):
        numbers = _numbers_in(line, profile)
        if numbers:
            report.coin_amount = numbers[-1]
        return

    if profile.reject_keyword and profile.reject_keyword.lower() in lower:
        numbers = _numbers_in(line, profile)
        if numbers:
            report.reject_quantity = int(numbers[0])
        return

    # A column header carries two or more labels and no data.
    if _is_column_header(lower, profile):
        return

    match = profile.match_denomination(line.replace("|", " ").split())
    if match is not None:
        denom, remainder = match
        _parse_denomination(line, denom, remainder, profile, report)
        return

    # Totals must be tested before subtotals only if one label is a prefix of
    # the other; test the more specific (longer) label first to be safe.
    for key, qty_attr, val_attr in (
        ("subtotals", "subtotal_quantity", "subtotal_value"),
        ("totals", "total_quantity", "total_value"),
    ):
        label = profile.label(key)
        if label and _is_label_line(lower, label):
            numbers = _numbers_in(line, profile)
            if not numbers:
                report.warnings.append(f"{key.capitalize()} line had no numbers: {line!r}")
                return
            if len(numbers) == 1 and key == "subtotals":
                # A lone figure on a SubTotal line is a money total, not a
                # count ("SubTotal 490") -- unlike Total, real BPS C1
                # printouts never carry a piece count on this line.
                setattr(report, val_attr, numbers[0])
                return
            setattr(report, qty_attr, int(numbers[0]))
            if len(numbers) > 1:
                setattr(report, val_attr, numbers[-1])
            return

    report.unmatched_lines.append(line)


def _is_label_line(lower_line: str, label: str) -> bool:
    return bool(label) and lower_line.startswith(label.lower())


def _label_value(line: str, label: str) -> str:
    return line[len(label):].strip(" :\t-").strip()


def _is_column_header(lower_line: str, profile: DeviceProfile) -> bool:
    header_labels = [
        profile.label(k) for k in ("denomination", "quantity", "value") if profile.label(k)
    ]
    hits = sum(1 for lab in header_labels if lab.lower() in lower_line)
    return hits >= 2 and not any(ch.isdigit() for ch in lower_line)


def _parse_denomination(line, denom, remainder, profile: DeviceProfile, report: ParsedReport) -> None:
    numbers = _numbers_in(" ".join(remainder), profile)
    if not numbers:
        report.warnings.append(
            f"Denomination {denom.device} had no quantity on line: {line!r}"
        )
        return

    quantity = int(numbers[0])
    if denom.nexus in report.denominations:
        report.warnings.append(
            f"Denomination {denom.device} appeared more than once; summing."
        )
        report.denominations[denom.nexus] += quantity
    else:
        report.denominations[denom.nexus] = quantity

    printed_value = numbers[-1] if len(numbers) > 1 else None
    expected = denom.value * quantity
    if printed_value is not None and printed_value != expected:
        report.warnings.append(
            f"{denom.device}: printed amount {printed_value} does not match "
            f"{quantity} x {denom.value} = {expected}."
        )
    report.values[denom.nexus] = printed_value if printed_value is not None else expected


# ── numbers ──────────────────────────────────────────────────────────────────


def _numbers_in(text: str, profile: DeviceProfile) -> list[Decimal]:
    found: list[Decimal] = []
    for token in text.replace("|", " ").split():
        if not _NUMERIC.match(token):
            continue
        try:
            found.append(_to_decimal(token, profile.decimal_separator))
        except InvalidOperation:
            continue
    return found


def _to_decimal(token: str, decimal_separator: str) -> Decimal:
    token = token.replace(" ", "").replace("'", "").replace(" ", "")
    sep = decimal_separator
    if sep == "auto":
        sep = _guess_separator(token)

    if sep == ",":
        token = token.replace(".", "").replace(",", ".")
    elif sep == ".":
        token = token.replace(",", "")
    else:
        token = token.replace(",", "").replace(".", "")
    return Decimal(token)


def _guess_separator(token: str) -> str:
    """Decide whether ',' or '.' is the decimal mark in a single token."""
    last_comma, last_dot = token.rfind(","), token.rfind(".")
    if last_comma == -1 and last_dot == -1:
        return ""
    if last_comma > -1 and last_dot > -1:
        return "," if last_comma > last_dot else "."

    sep = "," if last_comma > -1 else "."
    tail = token.split(sep)[-1]
    # Exactly three trailing digits with a single separator is a thousands
    # group ("1.200"), not a decimal fraction.
    if len(tail) == 3 and token.count(sep) >= 1 and len(token.split(sep)) == 2:
        return ""
    return sep


# ── validation ───────────────────────────────────────────────────────────────


def detect_profile(
    text: str, profiles: list[DeviceProfile] | None = None
) -> tuple[DeviceProfile, ParsedReport]:
    """Pick the profile that best explains a report.

    Used to identify an unknown machine from a capture, and as a fallback when
    the configured profile cannot parse what arrived. A profile whose totals
    agree with its own denomination lines always beats one that merely matched
    more rows, since agreeing totals are very hard to hit by accident.
    """
    candidates = profiles if profiles is not None else load_all_profiles()
    if not candidates:
        raise ReportParseError("No device profiles are installed.")

    scored: list[tuple[tuple, DeviceProfile, ParsedReport]] = []
    for profile in candidates:
        try:
            report = parse_report(text, profile)
        except ReportParseError:
            continue
        score = (
            0 if report.warnings else 1,
            len(report.denominations),
            -len(report.unmatched_lines),
        )
        scored.append((score, profile, report))

    if not scored:
        raise ReportParseError(
            "No installed profile could parse this report. Capture it with "
            "'python -m hardware.capture' and add a matching profile under "
            "hardware/profiles/."
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    _, profile, report = scored[0]
    return profile, report


def _validate_totals(report: ParsedReport) -> None:
    """Cross-check the machine's own totals against the parsed rows.

    This mirrors what ISA does and is the main guard against a partially read
    or mis-parsed report being accepted as a real count.
    """
    if report.total_quantity is not None and report.total_quantity != report.counted_quantity:
        report.warnings.append(
            f"Total quantity mismatch: report says {report.total_quantity}, "
            f"denomination lines sum to {report.counted_quantity}."
        )
    if report.total_value is not None and report.total_value != report.counted_value:
        report.warnings.append(
            f"Total value mismatch: report says {report.total_value}, "
            f"denomination lines plus coin sum to {report.counted_value}."
        )
    if report.subtotal_value is not None and report.subtotal_value != report.counted_note_value:
        report.warnings.append(
            f"Subtotal mismatch: report says {report.subtotal_value}, "
            f"denomination lines sum to {report.counted_note_value}."
        )
