from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from backend.core.catalogs import CatalogCode, CATALOG_DISPLAY_NAMES
from ..templating import templates
from ..deps import WebCurrentUser

router = APIRouter(tags=["web-catalog"])


@router.get("/catalog")
async def catalog_select_form(request: Request, current_user: WebCurrentUser):
    catalogs = [{"code": c.value, "display_name": CATALOG_DISPLAY_NAMES[c]} for c in CatalogCode]
    return templates.TemplateResponse(request, "catalog_select.html", {"catalogs": catalogs, "user": current_user})


@router.post("/catalog/select")
async def catalog_select_submit(request: Request, current_user: WebCurrentUser, code: str = Form(...)):
    try:
        catalog_code = CatalogCode(code)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown catalog '{code}'")

    request.session["catalog"] = catalog_code.value
    landing = "/web/transactions/new" if current_user.role.value == "cashier" else "/web/dashboard"
    return RedirectResponse(landing, status_code=303)
