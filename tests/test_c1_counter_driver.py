"""
Tests for the C1Counter serial driver (hardware/__init__.py), using a fake
serial port. Only the physical port is faked -- everything else is the
exact code path that runs against real hardware.
"""

import threading
import time as _time

import pytest

from hardware import C1Counter
from hardware.c1_engine import DuplicateReportError, ReportParseError


class FakeSerial:
    """Serial port that yields queued chunks, then silence.

    Silence is what the driver uses to detect the end of a report, so the
    fake must return b"" once drained rather than blocking.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.is_open = True
        self.written = bytearray()
        self.timeout = 0.05

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


_REPORT = (
    b"NO:1\r\nCurrency: EUR\r\n50 X 10 = 500.00\r\n20 X 5 = 100.00\r\n"
    b"Total 15 = 600.00\r\n\x1b\x69"
)


def _counter(chunks, seen_path=None, tmp_path=None):
    counter = C1Counter()
    counter._port = FakeSerial(chunks)
    if seen_path is None:
        from hardware.c1_engine import SeenReports
        counter._seen = SeenReports((tmp_path or __import__("pathlib").Path("/tmp")) / "seen.json")
    return counter


@pytest.fixture
def counter(tmp_path):
    return _counter([_REPORT], tmp_path=tmp_path)


def test_reads_and_parses_a_complete_report(counter):
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 10, "€20": 5}


def test_report_split_across_several_reads_is_reassembled(tmp_path):
    third = len(_REPORT) // 3
    chunks = [_REPORT[:third], _REPORT[third:2 * third], _REPORT[2 * third:]]
    counter = _counter(chunks, tmp_path=tmp_path)
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 10, "€20": 5}


def test_driver_never_writes_to_the_port(counter):
    """Listen-only is what makes sharing the printer line with ISA safe."""
    counter.wait_for_count_result(timeout_seconds=5)
    assert counter._port.written == bytearray()


def test_silence_raises_timeout_rather_than_returning_zero_counts(tmp_path):
    counter = _counter([], tmp_path=tmp_path)
    with pytest.raises(TimeoutError):
        counter.wait_for_count_result(timeout_seconds=1)


def test_unparseable_data_raises_and_includes_the_raw_text(tmp_path):
    text = b"NO:1\r\nSELF TEST PASSED\r\nFirmware 3.11\r\n\x1b\x69"
    counter = _counter([text], tmp_path=tmp_path)
    with pytest.raises(ReportParseError) as exc:
        counter.wait_for_count_result(timeout_seconds=5)
    assert "SELF TEST PASSED" in str(exc.value)


def test_a_mismatched_report_is_always_rejected(tmp_path):
    """Strict validation is not configurable -- a report whose totals
    disagree with its own denomination lines is always rejected."""
    broken = b"NO:1\r\nCurrency: EUR\r\n50 X 10 = 500.00\r\nTotal 99 = 500.00\r\n\x1b\x69"
    counter = _counter([broken], tmp_path=tmp_path)
    with pytest.raises(ReportParseError, match="consistency"):
        counter.wait_for_count_result(timeout_seconds=5)


def test_a_repeated_report_is_rejected_as_a_duplicate(tmp_path):
    """The same physical report read twice (reprint, retried request, a
    second poll landing on the same batch) must not be booked twice."""
    counter = _counter([_REPORT, _REPORT], tmp_path=tmp_path)
    counter.wait_for_count_result(timeout_seconds=5)
    with pytest.raises(DuplicateReportError):
        counter.wait_for_count_result(timeout_seconds=5)


def test_busy_port_error_names_isa_as_the_likely_owner(monkeypatch):
    """The common on-site failure: ISA already owns the machine's port."""
    import serial

    def refuse(*_a, **_kw):
        raise serial.SerialException(
            "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
        )

    monkeypatch.setattr(serial, "Serial", refuse)
    with pytest.raises(ConnectionError) as exc:
        C1Counter().connect()

    message = str(exc.value)
    assert "ISA" in message
    assert "already in use" in message


def test_missing_port_error_points_at_the_checker(monkeypatch):
    import serial

    def missing(*_a, **_kw):
        raise serial.SerialException("could not open port 'COM9': FileNotFoundError(2)")

    monkeypatch.setattr(serial, "Serial", missing)
    with pytest.raises(ConnectionError, match="list-ports"):
        C1Counter().connect()


def test_end_marker_ends_the_report_without_waiting_out_the_idle_timeout(tmp_path):
    """ESC i should short-circuit the wait; the idle timeout is only a fallback."""
    counter = _counter([_REPORT], tmp_path=tmp_path)
    started = _time.monotonic()
    result = counter.wait_for_count_result(timeout_seconds=10)
    elapsed = _time.monotonic() - started

    assert result.denominations == {"€50": 10, "€20": 5}
    assert elapsed < 1.0


def test_coin_amount_is_merged_into_denominations_as_coins(tmp_path):
    text = b"NO:1\r\nCurrency: EUR\r\n50 X 10 = 500.00\r\nCoin 5.00\r\nTotal 10 = 505.00\r\n\x1b\x69"
    counter = _counter([text], tmp_path=tmp_path)
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations["€50"] == 10
    assert result.denominations["Coins"] == 5


def test_trailing_bytes_after_one_report_do_not_leak_into_the_next_read(tmp_path):
    """Bytes still queued after a report's own end marker (e.g. a trailing
    cut command) must not prefix the *next* wait_for_count_result() call and
    corrupt its totals."""

    class FlushingFakeSerial(FakeSerial):
        def reset_input_buffer(self):
            self._chunks.clear()

    report1 = _REPORT
    trailing_noise = b"50 X 1 = 50.00\r\n"
    report2 = b"NO:2\r\nCurrency: EUR\r\n10 X 4 = 40.00\r\nTotal 4 = 40.00\r\n\x1b\x69"

    counter = _counter([], tmp_path=tmp_path)
    counter._port = FlushingFakeSerial([])

    def deliver(data, delay):
        _time.sleep(delay)
        counter._port._chunks.append(data)

    threading.Thread(target=deliver, args=(report1, 0.02)).start()
    result1 = counter.wait_for_count_result(timeout_seconds=5)
    assert result1.denominations == {"€50": 10, "€20": 5}

    counter._port._chunks.append(trailing_noise)
    threading.Thread(target=deliver, args=(report2, 0.05)).start()

    result2 = counter.wait_for_count_result(timeout_seconds=5)
    assert result2.denominations == {"€10": 4}


def test_denomination_keys_match_the_web_form_fields(counter):
    from web.routes.transaction_entry_web import DENOM_FIELDS

    form_labels = {label for _, label in DENOM_FIELDS}
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert set(result.denominations) <= form_labels
