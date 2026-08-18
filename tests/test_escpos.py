"""
Tests for ESC/P stream cleanup, and an end-to-end check against the exact
report layout the standalone capture bridge proved against a real BPS C1
(NO:/ESC i framing, "20 X 10 = 200" denomination lines, a Coin line, and a
raster block embedded in the middle of the report).
"""

from decimal import Decimal

from hardware.escpos import strip_escpos
from hardware.report_parser import parse_report
from hardware.report_profile import load_profile

PROFILE = load_profile("bps_c1_eur")


def _raster(payload: bytes, mode: int = 0) -> bytes:
    columns = len(payload) // (3 if mode in (32, 33) else 1)
    nl, nh = columns & 0xFF, (columns >> 8) & 0xFF
    return bytes([0x1B, ord("*"), mode, nl, nh]) + payload


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


def test_real_proven_report_format_parses_end_to_end():
    """The exact field layout captured from a real C1 by the standalone bridge."""
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
    report = parse_report(text, PROFILE)

    assert report.warnings == []
    assert report.denominations == {"€20": 10, "€10": 15, "€5": 28}
    assert report.currency == "EUR"
    assert report.report_number == "00002"
    assert report.user_id == "User1"
    assert report.machine_serial_number == "315190"
    assert report.reject_quantity == 2
    assert report.subtotal_value == Decimal("490")
    assert report.coin_amount == Decimal("0.00")
    assert report.total_quantity == 53
    assert report.total_value == Decimal("490")
    assert report.counted_value == Decimal("490")
