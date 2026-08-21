import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from hardware import open_shared_counter, close_shared_counter
from .core.config import settings
from .core.database import init_databases, CoreSessionLocal, core_engine, _catalog_engines
from .api.routes import auth, customers, transactions, catalogs, eod, notifications, duplicates, stats
from web.routes import (
    auth_web, catalog_web, dashboard_web, transactions_web, reports_web,
    eod_web, notifications_web, duplicates_web, stats_web, admin_web,
    transaction_entry_web,
)
from web.templating import templates

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _auto_seed():
    from sqlalchemy import select
    from .models.user import User
    import sys
    sys.path.insert(0, _PROJECT_ROOT)
    from database.seed import seed
    async with CoreSessionLocal() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            await seed()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_databases()
    await _auto_seed()
    await asyncio.to_thread(open_shared_counter)
    yield
    close_shared_counter()
    # Cleanly release pooled DB connections on shutdown. Without this the
    # engines opened in init_databases() are only ever reclaimed by garbage
    # collection, which SQLAlchemy warns about and which does not close
    # connections deterministically - harmless for local SQLite files but a
    # real leak pattern were this ever pointed at Postgres.
    await core_engine.dispose()
    for _engine in _catalog_engines.values():
        await _engine.dispose()


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
)

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


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.display_name}
