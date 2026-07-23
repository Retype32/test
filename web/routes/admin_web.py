import uuid
from typing import Annotated
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from backend.core.catalogs import CatalogCode
from backend.services.auth_service import AuthService
from backend.models.user import UserRole
from backend.schemas.user import UserCreate
from ..templating import templates
from ..deps import WebAdminOnly, WebCoreDB, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(prefix="/admin", tags=["web-admin"])


@router.get("/users")
async def admin_users_page(
    request: Request,
    current_user: WebAdminOnly,
    core_db: WebCoreDB,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = AuthService(core_db)
    users = await service.list_users()

    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({"active_nav": "admin_users", "users": users, "roles": [r.value for r in UserRole]})
    return templates.TemplateResponse(request, "admin_users.html", context)


@router.post("/users")
async def create_user(
    request: Request,
    current_user: WebAdminOnly,
    core_db: WebCoreDB,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
):
    service = AuthService(core_db)
    error_message = None
    success_message = None
    try:
        await service.create_user(UserCreate(username=username, password=password, role=UserRole(role)))
        success_message = f"User '{username}' created."
    except ValueError as e:
        error_message = str(e)

    users = await service.list_users()
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_users", "users": users, "roles": [r.value for r in UserRole],
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_users.html", context)


@router.post("/users/{user_id}/active")
async def set_user_active(
    user_id: uuid.UUID,
    current_user: WebAdminOnly,
    core_db: WebCoreDB,
    is_active: bool = Form(...),
):
    service = AuthService(core_db)
    try:
        await service.set_active(user_id, is_active, current_user.id)
    except ValueError:
        pass
    return RedirectResponse("/web/admin/users", status_code=303)
