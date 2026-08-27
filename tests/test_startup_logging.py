"""
Regression tests for startup logging surviving Alembic.

Real bug this covers: init_databases() runs the Alembic env.py on every app
startup, and that env.py applied alembic.ini's logging config. That config
declares ``[logger_root] level = WARNING`` with a single stderr handler, so
applying it mid-startup raised the root level to WARNING and replaced the
root handlers -- taking the rotating nexus.log file handler with them.
Every INFO the app logged from that point on vanished from both the console
and the log file, which meant a hardware fault during a live count left no
trace anywhere and was undiagnosable.
"""

import logging
from logging.handlers import RotatingFileHandler


def test_app_logging_is_marked_configured():
    from backend.core.logging_config import configure_logging, is_configured

    configure_logging()
    assert is_configured(), (
        "Alembic's env.py relies on this to know it must not apply "
        "alembic.ini's logging config over the app's own."
    )


def test_alembic_env_skips_filecconfig_when_the_app_configured_logging():
    """Both env.py files must gate fileConfig on is_configured()."""
    for path in ("alembic/env.py", "alembic_catalog/env.py"):
        source = open(path, encoding="utf-8").read()
        assert "not is_configured()" in source, f"{path} would clobber app logging"
        assert "disable_existing_loggers=False" in source, f"{path} would silence app loggers"


def test_root_logger_survives_a_stamp_at_startup(tmp_path):
    """The end-to-end property that actually matters: after the startup path
    has stamped the databases, INFO still reaches a file handler."""
    from backend.core.database import _stamp_head_sync
    from backend.core.logging_config import configure_logging

    configure_logging()
    root = logging.getLogger()
    before_level = root.level
    before_file_handlers = [
        h for h in root.handlers if isinstance(h, RotatingFileHandler)
    ]
    assert before_file_handlers, "expected the app's rotating file handler"

    _stamp_head_sync("alembic.ini", [])

    assert root.level == before_level, "alembic raised the root log level"
    assert [
        h for h in root.handlers if isinstance(h, RotatingFileHandler)
    ] == before_file_handlers, "alembic replaced the app's file handler"
    assert root.isEnabledFor(logging.INFO), "INFO logging was silently switched off"


def test_counter_startup_state_is_logged_loudly(caplog, monkeypatch):
    """Whether the counter came up is the first thing anyone needs when
    diagnosing "machine unavailable", so it must not be logged at a level
    another library's config can quietly drop."""
    import hardware

    monkeypatch.setattr(hardware, "COUNTER_COM_PORT", "")
    monkeypatch.setattr(hardware, "_shared", None)
    with caplog.at_level(logging.WARNING, logger="hardware"):
        hardware.open_shared_counter()
    assert any("CASH COUNTER DISABLED" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(hardware, "COUNTER_COM_PORT", "COM_TEST")
    monkeypatch.setattr(hardware, "get_counter", lambda: (_ for _ in ()).throw(
        ConnectionError("no such port")
    ))
    with caplog.at_level(logging.WARNING, logger="hardware"):
        hardware.open_shared_counter()
    assert any("CASH COUNTER NOT CONNECTED" in r.message for r in caplog.records)


def test_a_blank_com_port_defaults_to_com1_rather_than_disabling_the_counter():
    """Regression: dropping the device profile removed the COM1 fallback it
    used to supply, so an .env that never named a port silently disabled the
    counter instead of behaving as it always had. COM1 is also what the
    standalone C1 Check.py defaults to."""
    import importlib

    import hardware.config as counter_config

    for raw, expected in (("", "COM1"), ("  ", "COM1"), ("COM7", "COM7"), ("none", "")):
        import os

        os.environ["COUNTER_COM_PORT"] = raw
        importlib.reload(counter_config)
        assert counter_config.COUNTER_COM_PORT == expected, f"{raw!r} -> expected {expected!r}"
    os.environ.pop("COUNTER_COM_PORT", None)
    importlib.reload(counter_config)
