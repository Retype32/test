import uuid
from typing import Annotated
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from backend.core.catalogs import CatalogCode
from backend.services.auth_service import AuthService
from backend.services import backup_service
from backend.repositories.core_audit_repository import CoreAuditRepository
from backend.models.user import UserRole
from backend.schemas.user import UserCreate, UserUpdate
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


@router.post("/users/{user_id}/edit")
async def edit_user(
    request: Request,
    user_id: uuid.UUID,
    current_user: WebAdminOnly,
    core_db: WebCoreDB,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
    username: str = Form(...),
    password: str = Form(""),
    role: str = Form(...),
):
    service = AuthService(core_db)
    error_message = None
    success_message = None
    try:
        await service.update_user(
            user_id,
            UserUpdate(username=username, password=password or None, role=UserRole(role)),
            current_user.id,
        )
        success_message = f"User '{username}' updated."
    except ValueError as e:
        error_message = str(e)

    users = await service.list_users()
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_users", "users": users, "roles": [r.value for r in UserRole],
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_users.html", context)


@router.get("/backup")
async def admin_backup_page(
    request: Request,
    current_user: WebAdminOnly,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({"active_nav": "admin_backup", "backups": backup_service.list_backups()})
    return templates.TemplateResponse(request, "admin_backup.html", context)


@router.post("/backup")
async def create_backup(
    request: Request,
    current_user: WebAdminOnly,
    core_db: WebCoreDB,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    result = await backup_service.create_backup()
    audit_repo = CoreAuditRepository(core_db)
    await audit_repo.log(
        current_user.id,
        "DATABASE_BACKUP_CREATED",
        f"Backed up {len(result['copied'])} database(s) to {result['dir']}",
    )

    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_backup",
        "backups": backup_service.list_backups(),
        "success_message": f"Backup created: {len(result['copied'])} database(s) saved to backups/{result['timestamp']}/.",
    })
    return templates.TemplateResponse(request, "admin_backup.html", context)


@router.post("/users/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    current_user: WebAdminOnly,
    core_db: WebCoreDB,
    catalog_db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = AuthService(core_db)
    error_message = None
    success_message = None
    try:
        await service.delete_user(user_id, current_user.id)
        success_message = "User removed."
    except ValueError as e:
        error_message = str(e)

    users = await service.list_users()
    context = await build_nav_context(current_user, catalog_code, catalog_db)
    context.update({
        "active_nav": "admin_users", "users": users, "roles": [r.value for r in UserRole],
        "error_message": error_message, "success_message": success_message,
    })
    return templates.TemplateResponse(request, "admin_users.html", context)
