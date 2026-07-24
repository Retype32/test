import uuid
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from backend.services.auth_service import AuthService
from backend.repositories.user_repository import UserRepository
from ..templating import templates
from ..deps import WebCoreDB

router = APIRouter(tags=["web-auth"])


@router.get("/login")
async def login_form(request: Request, db: WebCoreDB):
    user_id_str = request.session.get("user_id")
    if user_id_str:
        user = await UserRepository(db).get_by_id(uuid.UUID(user_id_str))
        if user and user.is_active:
            landing = "/web/transactions/new" if user.role.value == "cashier" else "/web/dashboard"
            return RedirectResponse(landing if request.session.get("catalog") else "/web/catalog", status_code=303)
        request.session.clear()
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
async def login_submit(
    request: Request,
    db: WebCoreDB,
    username: str = Form(...),
    password: str = Form(...),
):
    service = AuthService(db)
    result = await service.login(username, password)
    if not result:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password."}, status_code=401
        )

    request.session["user_id"] = str(result.user.id)
    landing = "/web/transactions/new" if result.user.role.value == "cashier" else "/web/dashboard"
    if request.session.get("catalog"):
        return RedirectResponse(landing, status_code=303)
    return RedirectResponse("/web/catalog", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/web/login", status_code=303)
