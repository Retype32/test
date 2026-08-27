"""Central logging setup.

Modules log via the usual ``logging.getLogger(__name__)`` and rely on this
being called once, early, to attach handlers to the root logger. Safe to call
more than once (e.g. both the server entry point and the test suite import
``backend.main``) -- only the first call attaches handlers.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "nexus.log")

_configured = False


def is_configured() -> bool:
    """True once the app has set up its own logging.

    Alembic's env.py checks this before applying the logging config in
    alembic.ini. That config declares ``[logger_root] level = WARNING`` with
    a single stderr handler, and applying it mid-startup would replace the
    root handlers set up here -- taking the rotating nexus.log file handler
    with them and raising the root level to WARNING. Every INFO the app
    logged after startup then vanished, from both the console and the log
    file, which is exactly the state you do not want to debug hardware in.
    """
    return _configured


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(_LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # SQLAlchemy's own echo=True (settings.debug) already prints every SQL
    # statement to the console via its own handler; without this it would
    # also propagate into our file handler and drown out anything useful.
    logging.getLogger("sqlalchemy.engine").propagate = False
