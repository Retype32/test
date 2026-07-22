import uuid
from datetime import date
from typing import Optional
from fastapi import APIRouter, Query
from ...services.stats_service import StatsService
from ...schemas.stats import ProcessorStatResponse
from ..deps import SupervisorOrAbove, CatalogDB

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/processors", response_model=list[ProcessorStatResponse])
async def processor_stats_for_day(
    db: CatalogDB,
    _: SupervisorOrAbove,
    business_date: date = Query(...),
    user_id: Optional[uuid.UUID] = Query(None),
):
    service = StatsService(db)
    rows = await service.get_processor_stats(business_date, business_date, user_id)
    return [ProcessorStatResponse.model_validate(r) for r in rows]


@router.get("/processors/summary", response_model=list[ProcessorStatResponse])
async def processor_stats_summary(
    db: CatalogDB,
    _: SupervisorOrAbove,
    date_from: date = Query(...),
    date_to: date = Query(...),
    user_id: Optional[uuid.UUID] = Query(None),
):
    service = StatsService(db)
    rows = await service.get_processor_stats(date_from, date_to, user_id)
    return [ProcessorStatResponse.model_validate(r) for r in rows]
