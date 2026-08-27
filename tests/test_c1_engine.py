"""
Tests for hardware/c1_engine.py -- ESC/P cleanup, report parsing/validation,
stream framing and duplicate-report detection.

test_real_proven_report_format_parses_end_to_end below uses the exact field
layout the standalone C1 Checker (C1 Check.py) captured from a real BPS C1:
NO:/ESC i framing, "20 X 10 = 200" denomination lines, a Coin line, and a
raster block embedded in the middle of the report.
"""

import json

import pytest

from hardware.c1_engine import (
    DuplicateReportError,
    ReportParseError,
    SeenReports,
    StreamCollector,
    parse_and_validate,
    strip_escpos,
)


def _raster(payload: bytes, mode: int = 0) -> bytes:
    columns = len(payload) // (3 if mode in (32, 33) else 1)
    nl, nh = columns & 0xFF, (columns >> 8) & 0xFF
    return bytes([0x1B, ord("*"), mode, nl, nh]) + payload


# ── strip_escpos ─────────────────────────────────────────────────────────

def test_raster_block_is_skipped_by_its_declared_length():
    data = b"before" + _raster(b"\xff\xff\xff\xff") + b"after"
    assert strip_escpos(data) == "beforeafter"


def test_one_argument_commands_are_removed():
    data = b"a" + bytes([0x1B, ord("d"), 0x02]) + b"b"
    assert strip_escpos(data) == "ab"


def test_no_argument_commands_are_removed():
    data = b"a" + bytes([0x1B, ord("@")]) + bytes([0x1B, ord("i")]) + b"b"
    assert strip_escpos(data) == "ab"


def test_unknown_esc_command_drops_only_two_bytes():
    data = b"a" + bytes([0x1B, ord("Z"), ord("X")]) + b"b"
    assert strip_escpos(data) == "aXb"


def test_selected_control_bytes_become_spaces():
    data = b"a" + bytes([0x02, 0x03, 0x04, 0x1C]) + b"b"
    assert strip_escpos(data) == "a b"


def test_crlf_preserved_and_repeated_whitespace_collapsed():
    data = b"20   X    10\r\nSubTotal   490\r\n"
    assert strip_escpos(data) == "20 X 10\nSubTotal 490"


# ── parse_and_validate ───────────────────────────────────────────────────

def test_real_proven_report_format_parses_end_to_end():
    """The exact field layout captured from a real C1 by the standalone checker."""
    raw = (
        b"NO:00002\r\n"
        b"08-06-2026 15:33:28\r\n"
        b"User ID : User1\r\n"
        b"Machine SN : 315190\r\n"
        b"Currency: EUR\r\n"
        + _raster(b"\xff\xff\xff\xff") +
        b"20 X 10 = 200\r\n"
        b"10 X 15 = 150\r\n"
        b"5 X 28 = 140\r\n"
        b"SubTotal 490\r\n"
        b"Coin 0.00\r\n"
        b"Total 53 = 490\r\n"
        b"Reject Qty 2\r\n"
        b"\x1b\x69"
    )

    text = strip_escpos(raw)
    report = parse_and_validate(text)

    assert report.denominations == {"€20": 10, "€10": 15, "€5": 28}
    assert report.currency == "EUR"
    assert report.report_number == "00002"
    assert report.user_id == "User1"
    assert report.machine_serial_number == "315190"
    assert report.reject_quantity == 2
    assert report.subtotal == 490
    assert report.coin_amount == 0
    assert report.total_pieces == 53
    assert report.total_amount == 490
    assert report.fingerprint  # a non-empty hash was computed


def test_piece_mismatch_is_rejected_not_silently_accepted():
    text = "NO:1\nCurrency: EUR\n50 X 10 = 500.00\nTotal 99 = 500.00\n"
    with pytest.raises(ReportParseError, match="Piece mismatch"):
        parse_and_validate(text)


def test_amount_mismatch_is_rejected():
    text = "NO:1\nCurrency: EUR\n50 X 10 = 400.00\nTotal 10 = 500.00\n"
    with pytest.raises(ReportParseError, match="Amount mismatch"):
        parse_and_validate(text)


def test_row_internal_mismatch_is_rejected():
    text = "NO:1\nCurrency: EUR\n50 X 10 = 400.00\nTotal 10 = 400.00\n"
    with pytest.raises(ReportParseError, match="Invalid row"):
        parse_and_validate(text)


def test_reject_qty_is_captured_and_excluded_from_counts():
    text = "NO:1\nCurrency: EUR\n50 X 12 = 600.00\nReject Qty 3\nTotal 12 = 600.00\n"
    report = parse_and_validate(text)
    assert report.reject_quantity == 3
    assert report.denominations == {"€50": 12}


def test_european_number_format():
    text = "NO:1\nCurrency: EUR\n100 X 15 = 1.500,00\nTotal 15 = 1.500,00\n"
    report = parse_and_validate(text)
    assert report.denominations == {"€100": 15}
    assert report.total_amount == 1500


def test_coin_line_is_captured_and_included_in_the_total():
    text = "NO:1\nCurrency: EUR\n20 X 10 = 200.00\nCoin 5.00\nTotal 10 = 205.00\n"
    report = parse_and_validate(text)
    assert report.coin_amount == 5
    assert report.denominations == {"€20": 10}


def test_subtotal_mismatch_is_rejected():
    text = "NO:1\nCurrency: EUR\n20 X 10 = 200.00\nSubTotal 999.00\nTotal 10 = 200.00\n"
    with pytest.raises(ReportParseError, match="Subtotal mismatch"):
        parse_and_validate(text)


def test_missing_report_number_is_rejected():
    text = "Currency: EUR\n20 X 10 = 200.00\nTotal 10 = 200.00\n"
    with pytest.raises(ReportParseError, match="Missing report number"):
        parse_and_validate(text)


def test_no_denomination_rows_is_rejected():
    with pytest.raises(ReportParseError):
        parse_and_validate("NO:1\nCurrency: EUR\nTotal 0 = 0\n")


def test_unrecognised_report_raises_rather_than_returning_zeros():
    with pytest.raises(ReportParseError):
        parse_and_validate("SELF TEST PASSED\nFirmware 3.11\nSensor calibration OK\n")


def test_empty_input_raises():
    with pytest.raises(ReportParseError):
        parse_and_validate("   \n\n  ")


# ── StreamCollector ──────────────────────────────────────────────────────

_SIMPLE_REPORT = b"NO:1\r\nCurrency: EUR\r\n50 X 1 = 50.00\r\nTotal 1 = 50.00\r\n\x1b\x69"


def test_collector_frames_a_report_delivered_in_one_chunk():
    collector = StreamCollector()
    reports = collector.add(_SIMPLE_REPORT)
    assert reports == [_SIMPLE_REPORT]


def test_collector_reassembles_a_report_split_across_chunks():
    collector = StreamCollector()
    third = len(_SIMPLE_REPORT) // 3
    assert collector.add(_SIMPLE_REPORT[:third]) == []
    assert collector.add(_SIMPLE_REPORT[third:2 * third]) == []
    assert collector.add(_SIMPLE_REPORT[2 * third:]) == [_SIMPLE_REPORT]


def test_stray_end_marker_in_leading_noise_does_not_truncate_the_report():
    """A coincidental ESC i in a raster/logo block ahead of the real report
    text must not be mistaken for the terminator (it precedes any "Total"
    text, so the real one -- which does -- must be the one that's trusted)."""
    noise_with_fake_marker = b"\x02" * 10 + b"\x1b\x69" + b"\x02" * 10
    collector = StreamCollector()
    assert collector.add(noise_with_fake_marker + _SIMPLE_REPORT) == [_SIMPLE_REPORT]


def test_idle_report_requires_totals_text_present():
    collector = StreamCollector()
    collector.add(b"NO:1\r\nCurrency: EUR\r\n")  # no "Total" yet
    collector.last_rx -= 10  # force the idle window to have elapsed
    assert collector.idle_report(2.0) is None


def test_idle_report_fires_once_totals_text_has_arrived():
    collector = StreamCollector()
    collector.add(b"NO:1\r\nCurrency: EUR\r\n50 X 1 = 50.00\r\nTotal 1 = 50.00\r\n")
    collector.last_rx -= 10
    result = collector.idle_report(2.0)
    assert result is not None
    assert b"Total" in result


def test_final_partial_returns_and_clears_whatever_is_buffered():
    collector = StreamCollector()
    collector.add(b"NO:1\r\nCurrency: EUR\r\n")
    partial = collector.final_partial()
    assert partial is not None
    assert collector.final_partial() is None


# ── SeenReports ──────────────────────────────────────────────────────────

def test_seen_reports_persists_across_instances(tmp_path):
    path = tmp_path / "seen.json"
    store = SeenReports(path)
    assert not store.contains("abc123")
    store.add("abc123")
    assert store.contains("abc123")

    reloaded = SeenReports(path)
    assert reloaded.contains("abc123")
    assert json.loads(path.read_text()) == ["abc123"]


def test_seen_reports_starts_empty_when_no_file_exists(tmp_path):
    store = SeenReports(tmp_path / "does-not-exist.json")
    assert not store.contains("anything")
