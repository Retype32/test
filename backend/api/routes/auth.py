import logging
import uuid
from fastapi import APIRouter, HTTPException, Request, status
from ...services.auth_service import AuthService
from ...schemas.user import LoginRequest, TokenResponse, UserCreate, UserUpdate, UserResponse, UserActiveUpdate
from ...core import rate_limiter
from ..deps import AdminOnly, CoreDB

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("nexus.auth")


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: CoreDB, request: Request):
    client_ip = rate_limiter.client_ip_from_request(request)
    remaining = rate_limiter.seconds_until_unlocked(data.username, client_ip)
    if remaining:
        logger.warning(
            "login blocked (lockout active): username=%s ip=%s remaining=%.0fs",
            data.username, client_ip, remaining,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {int(remaining) + 1}s.",
        )

    service = AuthService(db)
    result = await service.login(data.username, data.password)
    if not result:
        rate_limiter.record_failure(data.username, client_ip)
        logger.warning("login failed: username=%s ip=%s", data.username, client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    rate_limiter.record_success(data.username, client_ip)
    return result


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    data: UserCreate,
    db: CoreDB,
    _: AdminOnly,
):
    service = AuthService(db)
    try:
        user = await service.create_user(data)
        return UserResponse.model_validate(user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    db: CoreDB,
    _: AdminOnly,
):
    service = AuthService(db)
    users = await service.list_users()
    return [UserResponse.model_validate(u) for u in users]


@router.patch("/users/{user_id}/active", response_model=UserResponse)
async def set_user_active(
    user_id: uuid.UUID,
    data: UserActiveUpdate,
    db: CoreDB,
    admin: AdminOnly,
):
    service = AuthService(db)
    try:
        user = await service.set_active(user_id, data.is_active, admin.id)
        return UserResponse.model_validate(user)
    except ValueError as e:
        status_code = 400 if "your own account" in str(e) else 404
        raise HTTPException(status_code=status_code, detail=str(e))


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    db: CoreDB,
    admin: AdminOnly,
):
    service = AuthService(db)
    try:
        user = await service.update_user(user_id, data, admin.id)
        return UserResponse.model_validate(user)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    db: CoreDB,
    admin: AdminOnly,
):
    service = AuthService(db)
    try:
        await service.delete_user(user_id, admin.id)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
