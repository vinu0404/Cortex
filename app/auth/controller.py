import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, _bearer
from app.auth.db_models import User
from app.auth.manager import AuthManager
from app.auth.models import LoginRequest, RefreshRequest, RegisterRequest, UserResponse, UserUpdate
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/register", response_model=None)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        manager = AuthManager(db)
        tokens = await manager.register(body.email, body.password)
        return ok(tokens.model_dump(), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/login", response_model=None)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        manager = AuthManager(db)
        tokens = await manager.login(body.email, body.password)
        return ok(tokens.model_dump())
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/refresh", response_model=None)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        manager = AuthManager(db)
        tokens = await manager.refresh(body.refresh_token)
        return ok(tokens.model_dump())
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.post("/logout", response_model=None)
async def logout(
    body: RefreshRequest,
    credentials=Depends(_bearer),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AuthManager(db)
        await manager.logout(body.refresh_token, current_user.id, credentials.credentials)
        return ok(message="Logged out")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/me", response_model=None)
async def me(current_user: User = Depends(get_current_user)) -> JSONResponse:
    return ok(UserResponse.model_validate(current_user).model_dump(mode="json"))


@router.patch("/me", response_model=None)
async def update_me(
    body: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        user = await AuthManager(db).update_user_profile(current_user, body)
        return ok(UserResponse.model_validate(user).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/me/stats", response_model=None)
async def get_my_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return ok(await AuthManager(db).get_user_stats(current_user.id))


@router.get("/me/dashboard-analytics", response_model=None)
async def get_dashboard_analytics(
    days: int = Query(15, ge=1, le=365),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    if days not in {7, 15, 30}:
        return fail("VALIDATION_ERROR", "days must be one of 7, 15, or 30", 422)
    return ok(await AuthManager(db).get_dashboard_analytics(current_user.id, days=days))


@router.get("/me/recent-conversations", response_model=None)
async def get_recent_conversations(
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return ok(await AuthManager(db).get_recent_conversations(current_user.id, limit))
