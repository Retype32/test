from datetime import date
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ...services.eod_service import EODService
from ...schemas.eod import EODCloseRequest, EODReopenRequest, EODClosureResponse
from ..deps import CurrentUser, SupervisorOrAbove, AdminOnly, CatalogDB

router = APIRouter(prefix="/eod", tags=["eod"])


@router.post("/close", response_model=EODClosureResponse, status_code=201)
async def close_day(
    data: EODCloseRequest,
    db: CatalogDB,
    current_user: SupervisorOrAbove,
):
    service = EODService(db)
    try:
        closure = await service.close_day(data.business_date, current_user.id)
        return EODClosureResponse.model_validate(closure)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/reopen", response_model=EODClosureResponse)
async def reopen_day(
    data: EODReopenRequest,
    db: CatalogDB,
    current_user: AdminOnly,
):
    service = EODService(db)
    try:
        closure = await service.reopen_day(data.business_date, current_user.id, data.reason)
        return EODClosureResponse.model_validate(closure)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/status")
async def day_status(
    db: CatalogDB,
    _: CurrentUser,
    business_date: date = Query(...),
):
    service = EODService(db)
    closed = await service.is_day_closed(business_date)
    return {"business_date": business_date, "closed": closed}


@router.get("/", response_model=list[EODClosureResponse])
async def list_closures(
    db: CatalogDB,
    _: SupervisorOrAbove,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
):
    service = EODService(db)
    closures = await service.list_closures(date_from=date_from, date_to=date_to)
    return [EODClosureResponse.model_validate(c) for c in closures]
