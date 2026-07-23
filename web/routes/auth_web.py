from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from backend.services.auth_service import AuthService
from ..templating import templates
from ..deps import WebCoreDB

router = APIRouter(tags=["web-auth"])


@router.get("/login")
async def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/web/dashboard", status_code=303)
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

    if result.user.role.value == "cashier":
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "The web portal is available to supervisors and administrators only."},
            status_code=403,
        )

    request.session["user_id"] = str(result.user.id)
    if request.session.get("catalog"):
        return RedirectResponse("/web/dashboard", status_code=303)
    return RedirectResponse("/web/catalog", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/web/login", status_code=303)
