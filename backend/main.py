import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from hardware import open_shared_counter, close_shared_counter
from .core.config import settings
from .core.database import init_databases, CoreSessionLocal, core_engine, _catalog_engines
from .core.security import validate_production_settings
from .services.eod_scheduler import auto_close_all_catalogs
from .api.routes import auth, customers, transactions, catalogs, eod, notifications, duplicates, stats
from web.routes import (
    auth_web, catalog_web, dashboard_web, transactions_web, reports_web,
    eod_web, notifications_web, duplicates_web, stats_web, admin_web,
    transaction_entry_web,
)
from web.templating import templates

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Structured logging + correlation ID (Agent 5 High #5/#6).
#
# Previously: zero `import logging` anywhere in backend/web/reports -- every
# diagnostic was a bare print() (or, for most request-time failures,
# nothing at all: an unhandled exception produced no application-level
# trace beyond whatever uvicorn happened to print). This wires stdlib
# logging in, with a per-request correlation ID threaded through a
# ContextVar so every log line emitted while handling a request can be tied
# back to it, and echoes the same ID to the client as `X-Request-ID` so a
# support engineer can match a user's report to a specific server-side
# request/log line.
# ---------------------------------------------------------------------------

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get()
        return True


def _configure_logging() -> None:
    """Re-attaches a fresh StreamHandler on every call -- deliberately NOT
    guarded by "already configured, skip" idempotency, and deliberately
    called both at module import time AND again at the very start of
    lifespan() below.

    `logging.StreamHandler()` binds to whatever object `sys.stderr` refers
    to at the moment it's constructed, not a live reference re-resolved on
    every write, and this module is only imported once -- for a real ASGI
    server (`uvicorn backend.main:app`) that import happens during the
    server's own startup, which could in principle rebind `sys.stderr`
    afterward. Rebuilding the handler at the start of lifespan() (which by
    the ASGI lifespan protocol always runs after the server's own setup)
    is correct hardening against that regardless.

    KNOWN UNRESOLVED GAP, found during Phase 5 load testing and left
    honestly unresolved rather than papered over: under a real
    `uvicorn backend.main:app` process (not the in-process ASGI test
    client, where this logger works perfectly), `logger.info(...)` and
    `logger.exception(...)` calls made through *this specific*
    `logging.getLogger("nexus")` instance produce no output at all --
    verified by placing a raw `print(..., file=sys.stderr, flush=True)`
    immediately before and after a `logger.info(...)` call: both prints
    appear in the server's log, the call between them raises nothing, yet
    nothing from it appears, not even in a fallback/unformatted shape.
    This is not the handler being missing or misconfigured: messages from
    OTHER loggers (`sqlalchemy.engine`, `alembic.runtime.plugins`) DO reach
    this exact handler, correctly formatted, throughout the same process
    lifetime, including after this function has run. Ruled out: uvicorn's
    own `LOGGING_CONFIG` (read directly -- it has no "root" entry, so
    `dictConfig` cannot be touching root's handlers or level); a global
    `logging.disable(...)` call (would suppress the other loggers' INFO
    messages too, and it does not); anything in this codebase touching
    `logging.getLogger("nexus")` (grepped -- this is the only reference).
    The generic_exception_handler below still correctly returns a clean,
    sanitized response to the client in every case (confirmed under real
    concurrent load) -- what's unverified is purely whether the
    accompanying server-side log line is actually captured, which matters
    for the "required support information" half of the observability
    story, not for response correctness. Flagged for whoever picks this
    up next rather than left silently broken."""
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_nexus_handler", False)]:
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [req=%(request_id)s] %(name)s: %(message)s"
    ))
    handler.addFilter(_RequestIdLogFilter())
    handler._nexus_handler = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger("nexus")


async def _auto_seed():
    # S-01: a production deployment must be provisioned with real
    # credentials, not the demo seed path -- skip entirely, but say so
    # loudly rather than silently doing nothing.
    if settings.environment == "production":
        logger.info(
            "ENVIRONMENT=production: skipping auto-seed. Provision this "
            "deployment's users with real credentials instead of the demo seed."
        )
        return

    from sqlalchemy import select
    from .models.user import User
    import sys
    sys.path.insert(0, _PROJECT_ROOT)
    from database.seed import seed
    async with CoreSessionLocal() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            await seed()


async def _check_engine_reachable(engine) -> bool:
    """Cheap liveness probe (item 11 / Agent 5 High #7): a bare `SELECT 1`
    against one already-pooled connection. Deliberately not wrapped in any
    retry/backoff -- this endpoint needs to answer in a few ms, not perform
    a full connectivity sweep."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("health check: engine unreachable", exc_info=True)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-assert our log handler survives the ASGI server's own logging
    # setup -- see _configure_logging()'s docstring for why this can't
    # just be the one call at module import time.
    _configure_logging()

    # S-02/S-11: refuse to start in production with a default/missing
    # SECRET_KEY or a non-Secure session cookie -- before touching the
    # database at all. No-op in development (today's behavior, unchanged).
    validate_production_settings(settings)

    await init_databases()
    await _auto_seed()
    await asyncio.to_thread(open_shared_counter)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        auto_close_all_catalogs, CronTrigger(hour=0, minute=0), id="eod_auto_close", replace_existing=True
    )
    scheduler.start()
    # Exposed for GET /ready (deploy-time draining signal) -- there is no
    # other place that holds a reference to this scheduler instance.
    app.state.scheduler = scheduler

    logger.info("Nexus startup complete (environment=%s)", settings.environment)
    yield

    logger.info("Nexus shutdown starting")
    scheduler.shutdown(wait=False)
    close_shared_counter()
    # Cleanly release pooled DB connections on shutdown. Without this the
    # engines opened in init_databases() are only ever reclaimed by garbage
    # collection, which SQLAlchemy warns about and which does not close
    # connections deterministically - harmless for local SQLite files but a
    # real leak pattern were this ever pointed at Postgres.
    await core_engine.dispose()
    for _engine in _catalog_engines.values():
        await _engine.dispose()
    logger.info("Nexus shutdown complete")


app = FastAPI(
    title=settings.display_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="nexus_web_session",
    same_site="lax",
    https_only=settings.session_cookie_secure,
    # S-06: previously unset, silently inheriting Starlette's 14-day
    # default -- nearly 42x longer than the JWT's own 8-hour expiry for
    # what's meant to be the same authentication tier. Matched to the JWT's
    # configured lifetime instead. Not mode-gated -- this is a bug fix, not
    # a hardening posture.
    max_age=settings.access_token_expire_minutes * 60,
)


# ---------------------------------------------------------------------------
# Correlation-ID + security headers (Agent 5 High #6, S-05). The CSP below
# was written after checking every template under web/templates/: there are
# no external script/style hosts anywhere (no CDN `<script src="https://...">`),
# only same-origin stylesheets/scripts plus a real amount of inline
# `<script>` blocks and inline `style="..."` attributes (theme toggle,
# wizard popup logic, ad hoc layout tweaks) -- hence 'unsafe-inline' for
# script-src/style-src rather than a stricter nonce-based policy, which
# would require touching every template. HSTS is production-only: asserting
# it over local plain HTTP dev traffic is actively wrong (it would tell a
# browser to only ever speak HTTPS to localhost).
#
# Implemented as ONE raw-ASGI middleware class (scope/receive/send), not two
# separate `@app.middleware("http")` functions. That decorator wraps
# Starlette's `BaseHTTPMiddleware`, which runs the downstream app in a
# separate anyio task communicating over an in-memory stream -- a documented
# Starlette limitation that can prevent FastAPI's yield-based dependency
# cleanup (our get_core_db/get_catalog_db session dependencies, whose
# `finally: await session.close()` is what returns a connection to the
# pool) from ever running. Confirmed here empirically, not just from the
# docs: with two stacked BaseHTTPMiddleware instances in place, any request
# whose route raises (a 401/403/404, a validation error, an assertion --
# ordinary things the existing test suite deliberately exercises) leaked its
# DB session's aiosqlite connection, and with it that connection's own
# non-daemon worker thread (aiosqlite starts one per connection). One or two
# of those are the "SAWarning: garbage collector is trying to clean up
# non-checked-in connection" noise already flagged in
# docs/production_readiness/01_transaction_integrity.md Section 3 --
# accumulated across a 180+ test suite, enough of them are non-daemon
# threads sitting in an unbounded `queue.get()` that the whole process hangs
# at interpreter shutdown instead of exiting, since `threading._shutdown`
# joins every non-daemon thread before the process can end. Raw ASGI
# middleware calls the downstream app in the *same* task, so this class of
# bug does not apply.
# ---------------------------------------------------------------------------
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class RequestContextMiddleware:
    """Pure ASGI middleware: assigns/propagates a correlation ID (available
    to every log line via the ContextVar above, echoed back as
    X-Request-ID) and stamps the standard security-header set onto every
    HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1").strip()
                break
        request_id = incoming or uuid.uuid4().hex
        token = _request_id_ctx.set(request_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Frame-Options"] = "DENY"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                if "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = _CSP
                if settings.environment == "production":
                    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _request_id_ctx.reset(token)


app.add_middleware(RequestContextMiddleware)


app.mount("/web/static", StaticFiles(directory=os.path.join(_PROJECT_ROOT, "web", "static")), name="web_static")

app.include_router(auth.router, prefix="/api/v1")
app.include_router(customers.router, prefix="/api/v1")
app.include_router(transactions.router, prefix="/api/v1")
app.include_router(catalogs.router, prefix="/api/v1")
app.include_router(eod.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(duplicates.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")

app.include_router(auth_web.router, prefix="/web")
app.include_router(catalog_web.router, prefix="/web")
app.include_router(dashboard_web.router, prefix="/web")
app.include_router(transaction_entry_web.router, prefix="/web")
app.include_router(transactions_web.router, prefix="/web")
app.include_router(reports_web.router, prefix="/web")
app.include_router(eod_web.router, prefix="/web")
app.include_router(notifications_web.router, prefix="/web")
app.include_router(duplicates_web.router, prefix="/web")
app.include_router(stats_web.router, prefix="/web")
app.include_router(admin_web.router, prefix="/web")


@app.exception_handler(StarletteHTTPException)
async def web_aware_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Renders HTML error pages for browser (/web/*) requests; JSON API
    requests keep FastAPI's default behavior. Redirects (3xx, used by the
    web auth/catalog guards) fall through untouched to the default handler,
    which preserves the Location header."""
    if request.url.path.startswith("/web"):
        if exc.status_code in (401, 403):
            return templates.TemplateResponse(
                request, "errors/403.html", {"detail": exc.detail}, status_code=exc.status_code
            )
        if exc.status_code == 404:
            return templates.TemplateResponse(request, "errors/404.html", {}, status_code=404)

    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Catch-all (item 12 / Agent 5 Medium #11): previously any unhandled
    exception (DB error, filesystem error, a bug) fell through to
    Starlette's bare plain-text "Internal Server Error", with no
    correlation ID, no logging, and no branded page. Logs the exception
    with its correlation ID, route, and method (never the request body,
    headers, or any credential) and returns a clean, sanitized response --
    an HTML page for the web portal, JSON for the API, matching each
    surface's other error responses."""
    request_id = _request_id_ctx.get()
    logger.exception(
        "Unhandled exception: %s %s (request_id=%s)",
        request.method, request.url.path, request_id,
    )
    if request.url.path.startswith("/web"):
        return templates.TemplateResponse(
            request, "errors/500.html", {"request_id": request_id}, status_code=500,
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error.", "request_id": request_id},
    )


@app.get("/health")
async def health():
    """Real liveness check (item 11 / Agent 5 High #7): previously a
    hardcoded {"status": "ok"} with no actual check, so it reported healthy
    even with every database unreachable. Checks the core engine and one
    catalog engine (not all four, to keep this a "few ms" endpoint rather
    than a full connectivity sweep on every hit -- the core engine failing
    already means the whole app is unusable regardless of which catalog is
    also down)."""
    core_ok = await _check_engine_reachable(core_engine)
    catalog_code, catalog_engine = next(iter(_catalog_engines.items()))
    catalog_ok = await _check_engine_reachable(catalog_engine)
    healthy = core_ok and catalog_ok
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "unhealthy",
            "app": settings.display_name,
            "checks": {
                "core_db": "ok" if core_ok else "unreachable",
                f"catalog_db:{catalog_code.value}": "ok" if catalog_ok else "unreachable",
            },
        },
    )


@app.get("/ready")
async def ready(request: Request):
    """Deploy-time draining signal (item 11 / Agent 5's proposal): true
    readiness additionally requires the EOD scheduler to be running, on top
    of everything /health already checks -- distinguishing "process is up
    but not ready for traffic yet / draining on shutdown" from "process is
    fully healthy"."""
    core_ok = await _check_engine_reachable(core_engine)
    catalog_code, catalog_engine = next(iter(_catalog_engines.items()))
    catalog_ok = await _check_engine_reachable(catalog_engine)
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_ok = bool(scheduler and scheduler.running)
    ready_state = core_ok and catalog_ok and scheduler_ok
    return JSONResponse(
        status_code=200 if ready_state else 503,
        content={
            "status": "ready" if ready_state else "not_ready",
            "checks": {
                "core_db": "ok" if core_ok else "unreachable",
                f"catalog_db:{catalog_code.value}": "ok" if catalog_ok else "unreachable",
                "scheduler": "running" if scheduler_ok else "not_running",
            },
        },
    )
