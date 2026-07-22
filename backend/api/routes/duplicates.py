import uuid
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ...services.duplicate_detection_service import DuplicateDetectionService
from ...schemas.duplicate import DuplicateFlagResponse, DuplicateReviewRequest
from ..deps import SupervisorOrAbove, CatalogDB

router = APIRouter(prefix="/duplicates", tags=["duplicates"])


@router.get("/", response_model=list[DuplicateFlagResponse])
async def list_duplicates(
    db: CatalogDB,
    _: SupervisorOrAbove,
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    service = DuplicateDetectionService(db)
    flags = await service.list_flags(status=status, limit=limit)
    return [DuplicateFlagResponse.model_validate(f) for f in flags]


@router.get("/{flag_id}", response_model=DuplicateFlagResponse)
async def get_duplicate(flag_id: uuid.UUID, db: CatalogDB, _: SupervisorOrAbove):
    service = DuplicateDetectionService(db)
    flag = await service.get_flag(flag_id)
    if not flag:
        raise HTTPException(status_code=404, detail="Duplicate flag not found")
    return DuplicateFlagResponse.model_validate(flag)


@router.post("/{flag_id}/review", response_model=DuplicateFlagResponse)
async def review_duplicate(
    flag_id: uuid.UUID,
    data: DuplicateReviewRequest,
    db: CatalogDB,
    current_user: SupervisorOrAbove,
):
    service = DuplicateDetectionService(db)
    try:
        flag = await service.review_flag(flag_id, data.status, current_user.id, data.notes)
        return DuplicateFlagResponse.model_validate(flag)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
