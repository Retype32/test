import uuid
import json
from typing import Annotated
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from backend.core.catalogs import CatalogCode
from backend.services.duplicate_detection_service import DuplicateDetectionService
from ..templating import templates
from ..deps import WebSupervisorOrAbove, WebCatalogDB, get_web_catalog_code
from ..context import build_nav_context

router = APIRouter(tags=["web-duplicates"])


@router.get("/duplicates")
async def duplicates_page(
    request: Request,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    catalog_code: Annotated[CatalogCode, Depends(get_web_catalog_code)],
):
    service = DuplicateDetectionService(db)
    flags = await service.list_flags(status="pending", limit=200)

    resolved = []
    for flag in flags:
        txn = await service.txn_repo.get_by_id(flag.transaction_id)
        original = await service.txn_repo.get_by_id(flag.duplicate_of_transaction_id)
        resolved.append({
            "flag": flag,
            "txn": txn,
            "original": original,
            "matched_fields": ", ".join(json.loads(flag.matched_fields)),
        })

    context = await build_nav_context(current_user, catalog_code, db)
    context.update({"active_nav": "duplicates", "rows": resolved})
    return templates.TemplateResponse(request, "duplicates.html", context)


@router.post("/duplicates/{flag_id}/review")
async def review_duplicate(
    flag_id: uuid.UUID,
    current_user: WebSupervisorOrAbove,
    db: WebCatalogDB,
    status: str = Form(...),
    notes: str = Form(None),
):
    service = DuplicateDetectionService(db)
    try:
        await service.review_flag(flag_id, status, current_user.id, notes)
    except ValueError:
        pass
    return RedirectResponse("/web/duplicates", status_code=303)
