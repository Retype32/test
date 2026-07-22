import uuid
from typing import Annotated
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from ..core.database import get_core_db, get_catalog_db as _get_catalog_db
from ..core.catalogs import CatalogCode
from ..core.security import decode_access_token
from ..repositories.user_repository import UserRepository
from ..models.user import User, UserRole

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_core_db)],
) -> User:
    token = credentials.credentials
    user_id_str = decode_access_token(token)
    if not user_id_str:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


def require_role(*roles: UserRole):
    async def checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return checker


async def get_catalog_code(
    x_catalog: Annotated[str | None, Header(alias="X-Catalog")] = None,
) -> CatalogCode:
    if not x_catalog:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Catalog header is required")
    try:
        return CatalogCode(x_catalog.lower())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown catalog '{x_catalog}'")


async def get_catalog_db(
    code: Annotated[CatalogCode, Depends(get_catalog_code)],
) -> AsyncSession:
    async for session in _get_catalog_db(code):
        yield session


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminOnly = Annotated[User, Depends(require_role(UserRole.administrator))]
SupervisorOrAbove = Annotated[User, Depends(require_role(UserRole.supervisor, UserRole.administrator))]

CoreDB = Annotated[AsyncSession, Depends(get_core_db)]
CatalogDB = Annotated[AsyncSession, Depends(get_catalog_db)]
