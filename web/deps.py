import uuid
from typing import Annotated
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from backend.core.database import get_core_db, get_catalog_db as _get_catalog_db
from backend.core.catalogs import CatalogCode
from backend.repositories.user_repository import UserRepository
from backend.models.user import User, UserRole


def _redirect(location: str) -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": location})


async def get_web_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_core_db)],
) -> User:
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        raise _redirect("/web/login")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        request.session.clear()
        raise _redirect("/web/login")

    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if not user or not user.is_active:
        request.session.clear()
        raise _redirect("/web/login")
    return user


def require_web_role(*roles: UserRole):
    async def checker(current_user: Annotated[User, Depends(get_web_current_user)]) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return checker


async def get_web_catalog_code(request: Request) -> CatalogCode:
    code = request.session.get("catalog")
    if not code:
        raise _redirect("/web/catalog")
    try:
        return CatalogCode(code)
    except ValueError:
        request.session.pop("catalog", None)
        raise _redirect("/web/catalog")


async def get_web_catalog_db(
    code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
) -> AsyncSession:
    async for session in _get_catalog_db(code):
        yield session


WebCoreDB = Annotated[AsyncSession, Depends(get_core_db)]
WebCatalogDB = Annotated[AsyncSession, Depends(get_web_catalog_db)]
WebCurrentUser = Annotated[User, Depends(get_web_current_user)]
WebSupervisorOrAbove = Annotated[User, Depends(require_web_role(UserRole.supervisor, UserRole.administrator))]
WebAdminOnly = Annotated[User, Depends(require_web_role(UserRole.administrator))]
