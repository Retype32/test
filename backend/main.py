from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.config import settings
from .core.database import init_databases, CoreSessionLocal
from .api.routes import auth, customers, transactions, catalogs, eod, notifications, duplicates, stats


async def _auto_seed():
    from sqlalchemy import select
    from .models.user import User
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database.seed import seed
    async with CoreSessionLocal() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            await seed()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_databases()
    await _auto_seed()
    yield


app = FastAPI(
    title=settings.app_name,
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

app.include_router(auth.router, prefix="/api/v1")
app.include_router(customers.router, prefix="/api/v1")
app.include_router(transactions.router, prefix="/api/v1")
app.include_router(catalogs.router, prefix="/api/v1")
app.include_router(eod.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(duplicates.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}
