import uuid
from datetime import datetime, date
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ...services.transaction_service import TransactionService
from ...schemas.transaction import TransactionCreate, TransactionResponse, TransactionTransferRequest
from ..deps import CurrentUser, SupervisorOrAbove, AdminOnly, CatalogDB

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.post("/", response_model=TransactionResponse, status_code=201)
async def create_transaction(
    data: TransactionCreate,
    db: CatalogDB,
    current_user: CurrentUser,
):
    service = TransactionService(db)
    try:
        txn = await service.create_transaction(current_user.id, current_user.username, data)
        txn_full = await service.get_transaction(txn.transaction_id)
        return TransactionResponse.model_validate(txn_full)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=list[TransactionResponse])
async def list_transactions(
    db: CatalogDB,
    _: SupervisorOrAbove,
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    customer_id: Optional[str] = Query(None),
    location_id: Optional[str] = Query(None),
    user_id: Optional[uuid.UUID] = Query(None),
    business_date: Optional[date] = Query(None),
    limit: int = Query(500, le=1000),
    offset: int = Query(0, ge=0),
):
    service = TransactionService(db)
    transactions = await service.list_transactions(
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        location_id=location_id,
        user_id=user_id,
        business_date=business_date,
        limit=limit,
        offset=offset,
    )
    return [TransactionResponse.model_validate(t) for t in transactions]


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    db: CatalogDB,
    _: CurrentUser,
):
    service = TransactionService(db)
    txn = await service.get_transaction(transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return TransactionResponse.model_validate(txn)


@router.post("/{transaction_id}/transfer", response_model=TransactionResponse)
async def transfer_transaction(
    transaction_id: uuid.UUID,
    data: TransactionTransferRequest,
    db: CatalogDB,
    current_user: AdminOnly,
):
    service = TransactionService(db)
    try:
        txn = await service.transfer_business_date(
            transaction_id, data.new_business_date, data.reason, current_user.id
        )
        return TransactionResponse.model_validate(txn)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=status_code, detail=str(e))
