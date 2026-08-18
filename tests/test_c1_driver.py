"""
Tests for the BPS C1 serial driver itself, using a fake serial port.

These cover the code path that will run against real hardware: idle-time
framing, decoding, profile fallback and strict-mode rejection. Only the
physical port is faked.
"""

import pytest

from hardware.gd_c1_report import GDC1ReportCounter
from hardware.report_parser import ReportParseError
from hardware.report_profile import load_profile
from hardware.simulate import DEFAULT_BATCH, build_report

PROFILE = load_profile("bps_c1_eur")
EXPECTED_QTY = sum(DEFAULT_BATCH.values())


class FakeSerial:
    """Serial port that yields queued chunks, then silence.

    Silence is what the driver uses to detect the end of a report, so the
    fake must return b"" once drained rather than blocking.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.is_open = True
        self.written = bytearray()
        self.timeout = 0.2

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, data):
        self.written.extend(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _counter(chunks, profile=PROFILE, idle=0.01):
    """A driver wired to a fake port, with a short idle window for speed."""
    import dataclasses

    profile = dataclasses.replace(profile, idle_timeout_seconds=idle)
    counter = GDC1ReportCounter(profile=profile)
    counter._port = FakeSerial(chunks)
    return counter


def _encoded(layout="standard"):
    return build_report(DEFAULT_BATCH, layout).encode("cp437", errors="replace")


def test_reads_and_parses_a_complete_report():
    counter = _counter([_encoded()])
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert sum(result.denominations.values()) == EXPECTED_QTY
    assert result.denominations["€500"] == DEFAULT_BATCH["EUR 500"]


def test_report_split_across_several_reads_is_reassembled():
    """A real port delivers a report in arbitrary chunks, not one buffer."""
    data = _encoded()
    third = len(data) // 3
    chunks = [data[:third], data[third:2 * third], data[2 * third:]]
    counter = _counter(chunks)
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert sum(result.denominations.values()) == EXPECTED_QTY


def test_driver_never_writes_to_the_port():
    """Listen-only is what makes sharing the printer line with ISA safe."""
    counter = _counter([_encoded()])
    counter.wait_for_count_result(timeout_seconds=5)
    assert counter._port.written == bytearray()


def test_silence_raises_timeout_rather_than_returning_zero_counts():
    counter = _counter([])
    with pytest.raises(TimeoutError):
        counter.wait_for_count_result(timeout_seconds=1)


def test_noise_below_minimum_length_is_not_treated_as_a_report():
    counter = _counter([b"\x00\x00"])
    with pytest.raises(TimeoutError):
        counter.wait_for_count_result(timeout_seconds=1)


def test_unparseable_data_raises_and_includes_the_raw_text():
    counter = _counter([b"SELF TEST PASSED\r\nFirmware 3.11\r\nCalibration OK\r\n"])
    with pytest.raises(ReportParseError) as exc:
        counter.wait_for_count_result(timeout_seconds=5)
    assert "SELF TEST PASSED" in str(exc.value)


def test_strict_mode_rejects_a_report_whose_totals_disagree(monkeypatch):
    monkeypatch.setattr("hardware.gd_c1_report.COUNTER_STRICT", True)
    broken = (
        "Currency  EUR\r\n"
        "EUR 50   10    500.00\r\n"
        "Total    99    500.00\r\n"
    ).encode("cp437")
    counter = _counter([broken])
    with pytest.raises(ReportParseError, match="consistency"):
        counter.wait_for_count_result(timeout_seconds=5)


def test_non_strict_mode_accepts_a_mismatched_report_with_a_warning(monkeypatch):
    monkeypatch.setattr("hardware.gd_c1_report.COUNTER_STRICT", False)
    broken = (
        "Currency  EUR\r\n"
        "EUR 50   10    500.00\r\n"
        "Total    99    500.00\r\n"
    ).encode("cp437")
    counter = _counter([broken])
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 10}


def test_busy_port_error_names_isa_as_the_likely_owner(monkeypatch):
    """The common on-site failure: ISA already owns the machine's port."""
    import serial

    def refuse(*_a, **_kw):
        raise serial.SerialException(
            "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
        )

    monkeypatch.setattr(serial, "Serial", refuse)
    with pytest.raises(ConnectionError) as exc:
        GDC1ReportCounter(profile=PROFILE).connect()

    message = str(exc.value)
    assert "ISA" in message
    assert "already in use" in message


def test_missing_port_error_points_at_list_ports(monkeypatch):
    import serial

    def missing(*_a, **_kw):
        raise serial.SerialException("could not open port 'COM9': FileNotFoundError(2)")

    monkeypatch.setattr(serial, "Serial", missing)
    with pytest.raises(ConnectionError, match="list-ports"):
        GDC1ReportCounter(profile=PROFILE).connect()


def test_end_marker_ends_the_report_without_waiting_out_the_idle_timeout():
    """ESC i should short-circuit the wait; the idle timeout is only a fallback."""
    import time as _time

    data = _encoded() + b"\x1b\x69"
    counter = _counter([data], idle=5)  # idle is deliberately much longer than this test
    started = _time.monotonic()
    result = counter.wait_for_count_result(timeout_seconds=10)
    elapsed = _time.monotonic() - started

    assert sum(result.denominations.values()) == EXPECTED_QTY
    assert elapsed < 1.0


def test_coin_amount_is_merged_into_denominations_as_coins():
    text = "Currency EUR\r\nEUR 50   10   500.00\r\nCoin 5.00\r\nTotal 10   505.00\r\n"
    counter = _counter([text.encode("cp437")])
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations["€50"] == 10
    assert result.denominations["Coins"] == 5


def test_denomination_keys_match_the_web_form_fields():
    """The parser's output keys feed the transaction entry page directly."""
    from web.routes.transaction_entry_web import DENOM_FIELDS

    form_labels = {label for _, label in DENOM_FIELDS}
    counter = _counter([_encoded()])
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert set(result.denominations) <= form_labels
