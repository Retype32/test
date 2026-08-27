import uuid
from datetime import datetime, date
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from ...services.transaction_service import TransactionService
from ...schemas.transaction import (
    TransactionCreate, TransactionResponse, TransactionTransferRequest, TransactionCorrectRequest,
)
from ...core.catalogs import CatalogCode
from ...core.config import settings
from ...models.user import User, UserRole
from ..deps import CurrentUser, SupervisorOrAbove, AdminOnly, CatalogDB, get_catalog_code

router = APIRouter(prefix="/transactions", tags=["transactions"])

# Optional client-supplied retry-safety key (C-1 / consolidated plan §7).
# Declared once and reused on every state-changing route below so both
# surfaces (create, correct) share one contract.
IdempotencyKeyHeader = Annotated[Optional[str], Header(alias="Idempotency-Key")]


@router.post("/", response_model=TransactionResponse, status_code=201)
async def create_transaction(
    data: TransactionCreate,
    db: CatalogDB,
    current_user: CurrentUser,
    response: Response,
    catalog_code: Annotated[CatalogCode, Depends(get_catalog_code)],
    idempotency_key: IdempotencyKeyHeader = None,
):
    service = TransactionService(db)
    try:
        txn = await service.create_transaction(
            current_user.id, current_user.username, data,
            idempotency_key=idempotency_key, catalog=catalog_code.value,
        )
        # §7: "a duplicate key within scope returns the previously-created
        # transaction (200), not a new insert" -- overrides the route's
        # default 201 only for that replay case.
        if getattr(txn, "_idempotent_replay", False):
            response.status_code = 200
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


async def _get_transaction_by_id_guard(current_user: CurrentUser) -> User:
    """S-12: the API's single-transaction read used to allow any
    authenticated role (any cashier could read any transaction's UUID
    directly), inconsistent with the web portal and the list endpoint,
    which are both supervisor+. Tightened to match here -- but only in
    production mode: the existing test suite has a passing, unmodifiable
    regression test (test_transactions_api.py::test_get_transaction_by_id)
    asserting a cashier CAN read their own transaction by id via this exact
    route in today's (development) configuration, so tightening this
    unconditionally would both violate "don't break the existing suite" and
    fall outside this phase's own no-mode-gating exceptions (which are
    limited to S-10/S-04/the max_age fix). Gating it here is the resolution
    consistent with the brief's overarching "opt-in production hardening,
    unchanged dev behavior" rule.
    """
    if settings.environment == "production" and current_user.role not in (
        UserRole.supervisor, UserRole.administrator,
    ):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return current_user


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    db: CatalogDB,
    _: Annotated[User, Depends(_get_transaction_by_id_guard)],
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


@router.post("/{transaction_id}/correct", response_model=TransactionResponse, status_code=201)
async def correct_transaction(
    transaction_id: uuid.UUID,
    data: TransactionCorrectRequest,
    db: CatalogDB,
    current_user: SupervisorOrAbove,
    response: Response,
    catalog_code: Annotated[CatalogCode, Depends(get_catalog_code)],
    idempotency_key: IdempotencyKeyHeader = None,
):
    # M-2: correct_transaction was previously reachable only from the web
    # portal (web/routes/transactions_web.py) -- this gives any pure-API
    # client the same capability, with the same idempotency-key contract as
    # create_transaction.
    service = TransactionService(db)
    try:
        txn = await service.correct_transaction(
            transaction_id, data, data.reason, current_user.id,
            idempotency_key=idempotency_key, catalog=catalog_code.value,
        )
        if getattr(txn, "_idempotent_replay", False):
            response.status_code = 200
        return TransactionResponse.model_validate(txn)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        message = str(e)
        if "not found" in message:
            status_code = 404
        elif "already been corrected" in message or "already corrected by another request" in message:
            status_code = 409
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=message)
