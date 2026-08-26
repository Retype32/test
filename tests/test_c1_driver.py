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
        self._in_waiting_exc = None  # set to an exception to simulate a dead device

    @property
    def in_waiting(self):
        if self._in_waiting_exc is not None:
            raise self._in_waiting_exc
        return 0

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
    """Listen-only is what makes it safe for Nexus to hold this port at all."""
    counter = _counter([_encoded()])
    counter.wait_for_count_result(timeout_seconds=5)
    assert counter._port.written == bytearray()


def test_is_connected_true_for_an_open_responsive_port():
    counter = _counter([])
    assert counter.is_connected() is True


def test_is_connected_false_before_connect_is_ever_called():
    counter = GDC1ReportCounter(profile=PROFILE)
    assert counter.is_connected() is False


def test_is_connected_false_after_disconnect():
    counter = _counter([])
    counter.disconnect()
    assert counter.is_connected() is False


def test_is_connected_false_when_the_device_has_silently_died():
    """A vanished USB device surfaces as a SerialException the moment
    anything touches the port at all, even a read-only status query --
    is_connected() must catch that rather than trusting a leftover
    is_open=True that a plain None check would miss entirely."""
    import serial

    counter = _counter([])
    counter._port._in_waiting_exc = serial.SerialException("device disappeared")
    assert counter.is_connected() is False


def test_is_connected_never_writes_to_the_port():
    counter = _counter([])
    counter.is_connected()
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


def test_busy_port_error_says_nexus_must_be_the_sole_owner(monkeypatch):
    """The common on-site failure: something else already owns the machine's
    port. Nexus is meant to be the only thing holding it, so the message
    should say that plainly instead of naming any particular other program."""
    import serial

    def refuse(*_a, **_kw):
        raise serial.SerialException(
            "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
        )

    monkeypatch.setattr(serial, "Serial", refuse)
    with pytest.raises(ConnectionError) as exc:
        GDC1ReportCounter(profile=PROFILE).connect()

    message = str(exc.value)
    assert "already in use" in message
    assert "Nexus needs to be" in message


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


def test_stray_end_marker_byte_pair_in_leading_noise_does_not_truncate_the_report():
    """A coincidental ESC 'i' in the logo/raster block ahead of the real text
    must not be mistaken for the report's real end marker (regression: this
    used to return a truncated, unparsed buffer as soon as the two bytes
    turned up anywhere, real report content or not)."""
    noise = b"\x02" * 20 + b"\x1b\x69" + b"\x02" * 20  # >= min_report_bytes, no report text
    real = _encoded() + b"\x1b\x69"
    counter = _counter([noise, real])
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert sum(result.denominations.values()) == EXPECTED_QTY


def test_trailing_bytes_after_one_report_do_not_leak_into_the_next_read():
    """Bytes still queued after a report's own end marker (e.g. a trailing
    cut command) must not prefix the *next* wait_for_count_result() call and
    corrupt its totals (regression: the port was never cleared between
    reads, so leftover bytes from report N silently merged into report N+1).

    FakeSerial's reset_input_buffer() is a no-op (other tests rely on that
    to keep their pre-queued chunks intact), so this test uses a variant
    that actually discards whatever is queued when it's called -- matching
    a real port, where reset_input_buffer() drops only what has arrived so
    far, not bytes the machine hasn't sent yet. Each report is therefore
    delivered by a background thread *after* a short delay, same as a real
    port: the read call is already blocked and past its own reset_input_buffer()
    call by the time the bytes turn up, so that call never has a chance to
    discard the very data it's waiting for.
    """
    import threading
    import time as _time

    class FlushingFakeSerial(FakeSerial):
        def reset_input_buffer(self):
            self._chunks.clear()

    report1 = _encoded() + b"\x1b\x69"
    trailing_noise = b"EUR 50   1    50.00\r\n"  # left in the port after report1's marker
    report2 = _encoded() + b"\x1b\x69"

    counter = _counter([], idle=0.01)
    counter._port = FlushingFakeSerial([])

    def deliver(data, delay):
        _time.sleep(delay)
        counter._port._chunks.append(data)

    threading.Thread(target=deliver, args=(report1, 0.02)).start()
    result1 = counter.wait_for_count_result(timeout_seconds=5)
    assert sum(result1.denominations.values()) == EXPECTED_QTY

    # Leftover bytes sitting in the port before the next read even starts.
    counter._port._chunks.append(trailing_noise)
    threading.Thread(target=deliver, args=(report2, 0.05)).start()

    result2 = counter.wait_for_count_result(timeout_seconds=5)
    assert sum(result2.denominations.values()) == EXPECTED_QTY
    assert result2.denominations["€50"] == DEFAULT_BATCH["EUR 50"]


def test_denomination_keys_match_the_web_form_fields():
    """The parser's output keys feed the transaction entry page directly."""
    from web.routes.transaction_entry_web import DENOM_FIELDS

    form_labels = {label for _, label in DENOM_FIELDS}
    counter = _counter([_encoded()])
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert set(result.denominations) <= form_labels
