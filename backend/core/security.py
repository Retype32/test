import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
import bcrypt
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from jose import JWTError, jwt
from .config import settings

# S-02: known shipped/documented default SECRET_KEY values that must never
# be accepted in production -- the literal default in this file's Settings
# class, plus the example value SETUP.md tells operators to copy.
_DEFAULT_SECRET_KEYS = {
    "change-this-in-production-a-very-long-secret-key-for-jwt",
    "brinks-nexus-super-secret-key-change-in-production-2026",
}
_MIN_SECRET_KEY_LENGTH = 32


def validate_production_settings(cfg=settings) -> None:
    """Startup guard (S-02, S-11). Called once from main.py's lifespan()
    before init_databases() / before any traffic is served. A complete
    no-op outside production mode -- today's permissive dev/test defaults
    (including the shipped SECRET_KEY and SESSION_COOKIE_SECURE=false) keep
    working with zero friction, exactly as the rollback strategy in
    docs/production_readiness/06_consolidated_plan.md §13 requires.

    Raises RuntimeError (not just a log warning) so a misconfigured
    production deployment refuses to start rather than silently serving
    traffic with a forgeable secret or a session cookie that can leak over
    plain HTTP.
    """
    if cfg.environment != "production":
        return

    key = cfg.secret_key or ""
    if not key:
        raise RuntimeError(
            "Refusing to start in production: SECRET_KEY is empty. Set a real, "
            "random secret (32+ characters) via the SECRET_KEY environment variable."
        )
    if key in _DEFAULT_SECRET_KEYS:
        raise RuntimeError(
            "Refusing to start in production: SECRET_KEY is still set to a shipped "
            "or documented default value. Generate a real, random secret and set it "
            "via the SECRET_KEY environment variable."
        )
    if len(key) < _MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"Refusing to start in production: SECRET_KEY must be at least "
            f"{_MIN_SECRET_KEY_LENGTH} characters long (got {len(key)})."
        )

    if not cfg.session_cookie_secure:
        raise RuntimeError(
            "Refusing to start in production: SESSION_COOKIE_SECURE must be true "
            "in production so the web session cookie carries the Secure flag. Set "
            "SESSION_COOKIE_SECURE=true (only once the portal is served over HTTPS)."
        )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def create_access_token(subject: Any, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload = {"sub": str(subject), "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload.get("sub")
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# CSRF protection for the web portal (S-03).
#
# A per-session random seed is stored in the (itsdangerous-signed)
# SessionMiddleware cookie on first use; the value handed to a template is
# that seed re-signed with itsdangerous under a dedicated salt, so a token
# posted back can be verified against the session without any server-side
# token store. Enforcement is gated to production mode -- see
# `validate_production_settings` above and config.py's `environment` field
# for why: this keeps the existing (CSRF-token-less) dev/test suite green
# while still closing the gap for a real deployment. The token is still
# rendered into every form in every mode, so flipping ENVIRONMENT=production
# needs no template changes.
# ---------------------------------------------------------------------------

_csrf_serializer = URLSafeSerializer(settings.secret_key, salt="nexus-csrf")
CSRF_SESSION_KEY = "_csrf_seed"
CSRF_FORM_FIELD = "csrf_token"


def generate_csrf_token(request: Request) -> str:
    """Returns the CSRF token for this browser session, minting the
    underlying per-session seed on first use. Safe to call repeatedly on the
    same request/session -- returns the same token until the seed changes
    (i.e. until `request.session.clear()` on login/logout)."""
    seed = request.session.get(CSRF_SESSION_KEY)
    if not seed:
        seed = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = seed
    return _csrf_serializer.dumps(seed)


def _csrf_token_matches_session(request: Request, token: Optional[str]) -> bool:
    if not token:
        return False
    seed = request.session.get(CSRF_SESSION_KEY)
    if not seed:
        return False
    try:
        unsigned = _csrf_serializer.loads(token)
    except BadSignature:
        return False
    return secrets.compare_digest(unsigned, seed)


async def require_csrf(request: Request) -> None:
    """FastAPI dependency: add to every state-changing web-portal POST route
    (alongside its existing role/auth dependency) to verify the hidden
    `csrf_token` form field against this session. A no-op outside production
    mode (see module docstring above) so the existing dev/test suite, which
    predates CSRF tokens, is unaffected."""
    if settings.environment != "production":
        return
    form = await request.form()
    token = form.get(CSRF_FORM_FIELD)
    if not _csrf_token_matches_session(request, token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing CSRF token. Please refresh the page and try again.",
        )
