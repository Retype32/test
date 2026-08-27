"""Login brute-force protection (S-04).

Deliberately small and self-contained: a module-level dict tracking
consecutive login failures per (username, client IP), with a fixed lockout
window once a threshold is hit. No new dependency (no slowapi/limits) --
appropriate for this single-process app.

NOT mode-gated: brute-force protection should exist in every environment,
development included (unlike the other Phase 4 checks, which default to
permissive dev behavior and only turn on in production).

IMPORTANT for future deployments: this state lives in this one process's
memory. The moment Nexus runs as more than one worker/process (multiple
uvicorn/gunicorn workers, multiple pods behind a load balancer), each
process keeps its own independent counters and this protection becomes
trivially bypassable by an attacker whose requests happen to land on
different workers. A multi-worker deployment MUST move this state to a
shared store (Redis, memcached, or a DB table) -- not attempted here, out of
scope for this single-process hardening pass.
"""
import time
from threading import Lock

MAX_CONSECUTIVE_FAILURES = 5
LOCKOUT_SECONDS = 60.0

_lock = Lock()
_state: dict[tuple[str, str], dict[str, float]] = {}


def _key(username: str, client_ip: str) -> tuple[str, str]:
    return ((username or "").strip().lower(), client_ip or "unknown")


def seconds_until_unlocked(username: str, client_ip: str) -> float:
    """Returns how many seconds remain in an active lockout for this
    (username, IP) pair, or 0.0 if not currently locked out."""
    with _lock:
        entry = _state.get(_key(username, client_ip))
        if not entry:
            return 0.0
        remaining = entry.get("locked_until", 0.0) - time.monotonic()
        return remaining if remaining > 0 else 0.0


def record_failure(username: str, client_ip: str) -> None:
    """Records one failed login attempt; locks the (username, IP) pair out
    for LOCKOUT_SECONDS once MAX_CONSECUTIVE_FAILURES is reached."""
    with _lock:
        key = _key(username, client_ip)
        entry = _state.setdefault(key, {"failures": 0.0, "locked_until": 0.0})
        entry["failures"] += 1
        if entry["failures"] >= MAX_CONSECUTIVE_FAILURES:
            entry["locked_until"] = time.monotonic() + LOCKOUT_SECONDS


def record_success(username: str, client_ip: str) -> None:
    """Clears any failure history for this (username, IP) pair on a
    successful login."""
    with _lock:
        _state.pop(_key(username, client_ip), None)


def client_ip_from_request(request) -> str:
    """Best-effort client IP extraction shared by both login surfaces."""
    return request.client.host if request.client else "unknown"
