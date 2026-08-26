"""Tests for the process-wide shared counter connection in hardware/__init__.py.

Regression coverage for a real bug: once the shared counter was only ever
opened at startup, a machine that was not ready in that exact moment (or
that dropped and came back) was reported as "not connected" for the rest of
the process's life, even though it was genuinely reachable again -- fixing
this required a full app restart in the field. wait_for_shared_count() must
instead retry the connection on demand.
"""

import serial

import hardware


class FakeCounter:
    """A CashCounter stand-in whose connect()/read behaviour is scripted.

    `alive` is what is_connected() reports; connect()/disconnect() keep it
    in sync automatically, but a test can also flip it directly to simulate
    a connection that still looks open yet has silently died -- exactly the
    case a bare "is _shared None" check can't catch but is_connected() can.
    """

    def __init__(self, connect_results, read_results=None):
        self._connect_results = list(connect_results)
        self._read_results = list(read_results or [])
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.alive = False

    def connect(self):
        self.connect_calls += 1
        outcome = self._connect_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.alive = True
        return outcome

    def disconnect(self):
        self.disconnect_calls += 1
        self.alive = False

    def is_connected(self):
        return self.alive

    def wait_for_count_result(self, timeout_seconds=120):
        outcome = self._read_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _reset_shared_state(monkeypatch):
    monkeypatch.setattr(hardware, "COUNTER_MODE", "mock")
    monkeypatch.setattr(hardware, "_shared", None)


def test_reconnects_on_demand_after_a_failed_startup_attempt(monkeypatch):
    """A count request must not stay stuck just because the very first
    connection attempt (e.g. at app startup) lost the race with the machine
    coming online."""
    _reset_shared_state(monkeypatch)
    counter = FakeCounter(
        connect_results=[ConnectionError("machine not ready yet")],
        read_results=[hardware.CountResult(denominations={"€50": 1})],
    )
    monkeypatch.setattr(hardware, "get_counter", lambda: counter)

    # Simulates the failed open_shared_counter() attempt at startup.
    hardware.open_shared_counter()
    assert hardware._shared is None

    # The machine is reachable now; a second attempt (this is what a later
    # count request does) must succeed instead of requiring a restart.
    counter._connect_results.append(True)
    result = hardware.wait_for_shared_count()
    assert result.denominations == {"€50": 1}
    assert hardware._shared is counter
    assert counter.connect_calls == 2


def test_the_drivers_specific_failure_reason_reaches_the_caller_intact(monkeypatch):
    """The wizard's status badge shows this string verbatim -- a generic
    "not connected" would hide whether the real cause is a wrong COM port,
    something else already holding it, or the machine genuinely being off.
    The driver (e.g. GDC1ReportCounter) already works this out; it must not
    be replaced with a vaguer message on the way up."""
    _reset_shared_state(monkeypatch)
    counter = FakeCounter(
        connect_results=[ConnectionError("Serial port COM3 does not exist on this PC.")]
    )
    monkeypatch.setattr(hardware, "get_counter", lambda: counter)

    try:
        hardware.wait_for_shared_count()
        assert False, "expected ConnectionError"
    except ConnectionError as exc:
        assert "COM3 does not exist" in str(exc)


def test_a_dead_port_is_dropped_so_the_next_request_reconnects(monkeypatch):
    """If the physical port dies mid-use (cable pulled, USB re-enumerated),
    the stale connection must not be retried forever -- the next request
    should open a fresh one instead of failing the same way every time."""
    _reset_shared_state(monkeypatch)
    dead = FakeCounter(
        connect_results=[True],
        read_results=[serial.SerialException("device disappeared")],
    )
    monkeypatch.setattr(hardware, "get_counter", lambda: dead)
    hardware.open_shared_counter()
    assert hardware._shared is dead

    try:
        hardware.wait_for_shared_count()
        assert False, "expected serial.SerialException"
    except serial.SerialException:
        pass

    assert hardware._shared is None
    assert dead.disconnect_calls == 1

    fresh = FakeCounter(
        connect_results=[True],
        read_results=[hardware.CountResult(denominations={"€20": 3})],
    )
    monkeypatch.setattr(hardware, "get_counter", lambda: fresh)
    result = hardware.wait_for_shared_count()
    assert result.denominations == {"€20": 3}


def test_counter_mode_none_never_touches_the_shared_state(monkeypatch):
    monkeypatch.setattr(hardware, "COUNTER_MODE", "none")
    monkeypatch.setattr(hardware, "_shared", None)

    hardware.open_shared_counter()
    assert hardware._shared is None

    try:
        hardware.wait_for_shared_count()
        assert False, "expected ConnectionError"
    except ConnectionError as exc:
        assert "is configured" in str(exc).lower()


def test_ping_reconnects_without_ever_blocking_for_a_batch(monkeypatch):
    """ping_shared_counter() is meant to run at the start of every
    transaction and after every batch -- it must prove the connection is
    alive without ever waiting for the machine to have something to say.
    FakeCounter would raise IndexError if this touched
    wait_for_count_result(), since no read_results were scripted."""
    _reset_shared_state(monkeypatch)
    counter = FakeCounter(connect_results=[True])
    monkeypatch.setattr(hardware, "get_counter", lambda: counter)

    hardware.ping_shared_counter()
    assert hardware._shared is counter
    assert counter.connect_calls == 1


def test_ping_surfaces_the_specific_failure_reason(monkeypatch):
    _reset_shared_state(monkeypatch)
    counter = FakeCounter(
        connect_results=[ConnectionError("Serial port COM3 does not exist on this PC.")]
    )
    monkeypatch.setattr(hardware, "get_counter", lambda: counter)

    try:
        hardware.ping_shared_counter()
        assert False, "expected ConnectionError"
    except ConnectionError as exc:
        assert "COM3 does not exist" in str(exc)


def test_ping_with_counter_mode_none_raises_not_configured(monkeypatch):
    monkeypatch.setattr(hardware, "COUNTER_MODE", "none")
    monkeypatch.setattr(hardware, "_shared", None)

    try:
        hardware.ping_shared_counter()
        assert False, "expected ConnectionError"
    except ConnectionError as exc:
        assert "is configured" in str(exc).lower()


def test_a_connection_that_fails_its_own_liveness_check_is_dropped_and_reopened(monkeypatch):
    """is_connected() catches what a bare None check can't: a connection
    that still looks open (never explicitly disconnected, nothing has
    raised yet) but whose physical other end is actually gone -- e.g. a USB
    device that silently stopped responding while idle between
    transactions. Both ping and a real count request must reconnect instead
    of trusting a stale handle."""
    _reset_shared_state(monkeypatch)
    stale = FakeCounter(connect_results=[True])
    monkeypatch.setattr(hardware, "get_counter", lambda: stale)
    hardware.open_shared_counter()
    assert hardware._shared is stale

    stale.alive = False  # a liveness check failing without ever raising

    fresh = FakeCounter(
        connect_results=[True],
        read_results=[hardware.CountResult(denominations={"€10": 5})],
    )
    monkeypatch.setattr(hardware, "get_counter", lambda: fresh)

    result = hardware.wait_for_shared_count()
    assert result.denominations == {"€10": 5}
    assert hardware._shared is fresh
    assert stale.disconnect_calls == 1


def test_ping_also_drops_a_connection_that_fails_its_liveness_check(monkeypatch):
    _reset_shared_state(monkeypatch)
    stale = FakeCounter(connect_results=[True])
    monkeypatch.setattr(hardware, "get_counter", lambda: stale)
    hardware.open_shared_counter()
    stale.alive = False

    fresh = FakeCounter(connect_results=[True])
    monkeypatch.setattr(hardware, "get_counter", lambda: fresh)

    hardware.ping_shared_counter()
    assert hardware._shared is fresh
    assert stale.disconnect_calls == 1
