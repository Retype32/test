"""
Tests for the ISA log-tailing driver.

The log line shapes come from the strings inside ISA's BPS C1 plugin
("Received : ", "Product Code = ", " , Quantity = ", " sent to ISA.").
Exact prefixes (timestamps, event types) are unknown until a real log is
captured, so lines here carry plausible prefixes and the regexes must not
care about them.
"""

import threading
import time

import pytest

from hardware.isa_log import ISALogCounter
from hardware.report_profile import load_profile

PROFILE = load_profile("bps_c1_eur")


def make_counter(tmp_path, **kw):
    kw.setdefault("idle_seconds", 0.2)
    kw.setdefault("poll_seconds", 0.02)
    return ISALogCounter(profile=PROFILE, log_dir=tmp_path, **kw)


def append(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()


def append_later(path, text, delay=0.05):
    t = threading.Timer(delay, append, args=(path, text))
    t.start()
    return t


def test_product_code_lines_become_counts(tmp_path):
    log = tmp_path / "DEVICE_LOG.LOG"
    log.write_text("2026-07-27 10:00:00 INFO startup\n", encoding="utf-8")

    counter = make_counter(tmp_path)
    counter.connect()

    append_later(log, (
        "2026-07-27 10:01:00 INFO Product Code = 10401 , Quantity = 12\n"
        "2026-07-27 10:01:00 INFO Product Code = 10301 , Quantity = 30\n"
        "2026-07-27 10:01:01 INFO 42 records sent to ISA.\n"
    ))
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 12, "€20": 30}


def test_only_batches_after_connect_are_seen(tmp_path):
    """Yesterday's log content must never be booked as today's count."""
    log = tmp_path / "DEVICE_LOG.LOG"
    log.write_text(
        "old INFO Product Code = 10701 , Quantity = 99\n"
        "old INFO 99 sent to ISA.\n",
        encoding="utf-8",
    )
    counter = make_counter(tmp_path)
    counter.connect()

    append_later(log, "new INFO Product Code = 10081 , Quantity = 3\n"
                      "new INFO sent to ISA.\n")
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€5": 3}


def test_received_raw_lines_are_parsed_when_no_product_codes(tmp_path):
    """At Debug level the plugin logs the raw serial lines it received."""
    log = tmp_path / "DEVICE_LOG.LOG"
    log.write_text("startup\n", encoding="utf-8")
    counter = make_counter(tmp_path)
    counter.connect()

    append_later(log, (
        "10:01 DEBUG Received : Currency  EUR\n"
        "10:01 DEBUG Received : EUR 50   12   600.00\n"
        "10:01 DEBUG Received : EUR 20   30   600.00\n"
        "10:01 DEBUG Received : Total    42  1200.00\n"
    ))
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 12, "€20": 30}


def test_batch_split_across_writes_is_accumulated(tmp_path):
    log = tmp_path / "DEVICE_LOG.LOG"
    log.write_text("", encoding="utf-8")
    counter = make_counter(tmp_path)
    counter.connect()

    append_later(log, "INFO Product Code = 10401 , Quantity = 10\n", delay=0.03)
    append_later(log, "INFO Product Code = 10401 , Quantity = 2\n", delay=0.08)
    append_later(log, "INFO sent to ISA.\n", delay=0.12)
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 12}


def test_rotation_to_a_new_log_file_is_followed(tmp_path):
    old = tmp_path / "DEVICE_LOG.LOG"
    old.write_text("startup\n", encoding="utf-8")
    counter = make_counter(tmp_path)
    counter.connect()

    def rotate():
        new = tmp_path / "DEVICE_LOG_2.LOG"
        new.write_text("INFO Product Code = 10161 , Quantity = 7\n"
                       "INFO sent to ISA.\n", encoding="utf-8")
        # Make the new file unambiguously newer.
        stamp = time.time() + 5
        import os
        os.utime(new, (stamp, stamp))

    threading.Timer(0.05, rotate).start()
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€10": 7}


def test_unknown_product_codes_do_not_silently_vanish(tmp_path, capsys):
    log = tmp_path / "DEVICE_LOG.LOG"
    log.write_text("", encoding="utf-8")
    counter = make_counter(tmp_path)
    counter.connect()

    append_later(log, "INFO Product Code = 99999 , Quantity = 4\n"
                      "INFO Product Code = 10401 , Quantity = 1\n"
                      "INFO sent to ISA.\n")
    result = counter.wait_for_count_result(timeout_seconds=5)
    assert result.denominations == {"€50": 1}
    assert "99999" in capsys.readouterr().out


def test_silence_raises_timeout(tmp_path):
    log = tmp_path / "DEVICE_LOG.LOG"
    log.write_text("startup\n", encoding="utf-8")
    counter = make_counter(tmp_path)
    counter.connect()
    with pytest.raises(TimeoutError, match="ISA"):
        counter.wait_for_count_result(timeout_seconds=1)


def test_missing_directory_fails_with_guidance():
    counter = ISALogCounter(profile=PROFILE, log_dir="C:/does/not/exist-xyz")
    with pytest.raises(ConnectionError, match="ISA_LOG_DIR"):
        counter.connect()


def test_empty_directory_fails_rather_than_waiting_forever(tmp_path):
    counter = make_counter(tmp_path)
    with pytest.raises(ConnectionError, match="No log files"):
        counter.connect()
