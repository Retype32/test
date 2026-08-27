"""
Tests for the C1Counter serial driver (hardware/__init__.py), using a fake
serial port. Only the physical port is faked -- everything else, including
the background reader thread, is the exact code path that runs against real
hardware.
"""

import threading
import time as _time

import pytest

from hardware import C1Counter
from hardware.c1_engine import ReportParseError, SeenReports


class FakeSerial:
    """Serial port that yields queued chunks, then silence.

    Silence is what the driver uses to detect the end of an unterminated
    report, so the fake must return b"" once drained rather than blocking.
    Reads are guarded by a lock because the driver now reads from a
    background thread while the test appends from the main one.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._lock = threading.Lock()
        self.is_open = True
        self.written = bytearray()
        self.timeout = 0.05

    def read(self, _size):
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        _time.sleep(0.01)  # mimic a real port's read timeout
        return b""

    def feed(self, data):
        with self._lock:
            self._chunks.append(data)

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
_REPORT_2 = b"NO:2\r\nCurrency: EUR\r\n10 X 4 = 40.00\r\nTotal 4 = 40.00\r\n\x1b\x69"


def _counter(chunks, tmp_path):
    """A driver wired to a fake port with its reader thread running."""
    counter = C1Counter()
    counter._seen = SeenReports(tmp_path / "seen.json")
    counter._port = FakeSerial(chunks)
    counter._start_reader()
    return counter


@pytest.fixture
def counter(tmp_path):
    c = _counter([_REPORT], tmp_path)
    yield c
    c.disconnect()


def test_reads_and_parses_a_complete_report(counter):
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 10, "€20": 5}


def test_report_split_across_several_reads_is_reassembled(tmp_path):
    third = len(_REPORT) // 3
    chunks = [_REPORT[:third], _REPORT[third:2 * third], _REPORT[2 * third:]]
    c = _counter(chunks, tmp_path)
    try:
        assert c.wait_for_count_result(timeout_seconds=5).denominations == {"€50": 10, "€20": 5}
    finally:
        c.disconnect()


def test_driver_never_writes_to_the_port(counter):
    """Listen-only is what makes sharing the printer line with ISA safe."""
    counter.wait_for_count_result(timeout_seconds=5)
    assert counter._port.written == bytearray()


def test_silence_raises_timeout_rather_than_returning_zero_counts(tmp_path):
    c = _counter([], tmp_path)
    try:
        with pytest.raises(TimeoutError):
            c.wait_for_count_result(timeout_seconds=1)
    finally:
        c.disconnect()


def test_a_batch_printed_before_anyone_waits_is_not_lost(tmp_path):
    """The whole point of listening continuously: the C1 prints when the
    operator ends a batch, which is routinely *before* the cashier reaches
    the Count Cash screen. That report must still be there when they do."""
    c = _counter([_REPORT], tmp_path)
    try:
        _time.sleep(0.2)  # the report arrives and is framed with nobody waiting
        result = c.wait_for_count_result(timeout_seconds=5)
        assert result.denominations == {"€50": 10, "€20": 5}
    finally:
        c.disconnect()


def test_several_batches_arriving_together_are_all_kept(tmp_path):
    """Two reports in one burst must both be queued -- returning the first
    and discarding the rest would silently lose a counted batch."""
    c = _counter([_REPORT + _REPORT_2], tmp_path)
    try:
        first = c.wait_for_count_result(timeout_seconds=5)
        second = c.wait_for_count_result(timeout_seconds=5)
        assert first.denominations == {"€50": 10, "€20": 5}
        assert second.denominations == {"€10": 4}
    finally:
        c.disconnect()


def test_unparseable_data_surfaces_as_an_error(tmp_path):
    text = b"NO:1\r\nSELF TEST PASSED\r\nFirmware 3.11\r\n\x1b\x69"
    c = _counter([text], tmp_path)
    try:
        with pytest.raises(ReportParseError) as exc:
            c.wait_for_count_result(timeout_seconds=5)
        assert "SELF TEST PASSED" in str(exc.value)
    finally:
        c.disconnect()


def test_a_mismatched_report_is_always_rejected(tmp_path):
    """Strict validation is not configurable -- a report whose totals
    disagree with its own denomination lines is always rejected."""
    broken = b"NO:1\r\nCurrency: EUR\r\n50 X 10 = 500.00\r\nTotal 99 = 500.00\r\n\x1b\x69"
    c = _counter([broken], tmp_path)
    try:
        with pytest.raises(ReportParseError, match="consistency"):
            c.wait_for_count_result(timeout_seconds=5)
    finally:
        c.disconnect()


def test_a_repeated_report_is_dropped_quietly_not_counted_twice(tmp_path):
    """The same physical report arriving twice (a receipt reprint, the
    machine repeating itself) must not be booked twice -- and must not
    interrupt the cashier with an error either, since the listener is
    running continuously and a repeat is not their problem to fix."""
    c = _counter([_REPORT, _REPORT], tmp_path)
    try:
        assert c.wait_for_count_result(timeout_seconds=5).denominations == {"€50": 10, "€20": 5}
        # The repeat is skipped, so the next wait finds nothing at all.
        with pytest.raises(TimeoutError):
            c.wait_for_count_result(timeout_seconds=1)
    finally:
        c.disconnect()


def test_a_dead_port_surfaces_the_real_error_to_a_waiting_request(tmp_path):
    import serial

    class DyingSerial(FakeSerial):
        def read(self, _size):
            raise serial.SerialException("device disappeared")

    c = C1Counter()
    c._seen = SeenReports(tmp_path / "seen.json")
    c._port = DyingSerial([])
    c._start_reader()
    try:
        with pytest.raises(serial.SerialException):
            c.wait_for_count_result(timeout_seconds=5)
    finally:
        c.disconnect()


def test_busy_port_error_names_the_likely_owners(monkeypatch):
    """The common on-site failure: C1 Check.py or ISA already owns the port."""
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
    assert "C1 Check.py" in message
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
    c = _counter([_REPORT], tmp_path)
    try:
        started = _time.monotonic()
        result = c.wait_for_count_result(timeout_seconds=10)
        elapsed = _time.monotonic() - started
        assert result.denominations == {"€50": 10, "€20": 5}
        assert elapsed < 1.0
    finally:
        c.disconnect()


def test_coin_amount_is_merged_into_denominations_as_coins(tmp_path):
    text = b"NO:1\r\nCurrency: EUR\r\n50 X 10 = 500.00\r\nCoin 5.00\r\nTotal 10 = 505.00\r\n\x1b\x69"
    c = _counter([text], tmp_path)
    try:
        result = c.wait_for_count_result(timeout_seconds=5)
        assert result.denominations["€50"] == 10
        assert result.denominations["Coins"] == 5
    finally:
        c.disconnect()


def test_a_later_report_is_picked_up_on_the_same_connection(tmp_path):
    """Consecutive batches on one connection must each come back intact,
    with nothing from the first leaking into the second."""
    c = _counter([], tmp_path)
    try:
        c._port.feed(_REPORT)
        assert c.wait_for_count_result(timeout_seconds=5).denominations == {"€50": 10, "€20": 5}
        c._port.feed(_REPORT_2)
        assert c.wait_for_count_result(timeout_seconds=5).denominations == {"€10": 4}
    finally:
        c.disconnect()


def test_denomination_keys_match_the_web_form_fields(counter):
    from web.routes.transaction_entry_web import DENOM_FIELDS

    form_labels = {label for _, label in DENOM_FIELDS}
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert set(result.denominations) <= form_labels
