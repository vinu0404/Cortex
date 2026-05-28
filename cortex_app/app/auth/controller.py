import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, _bearer
from app.auth.db_models import User
from app.auth.manager import AuthManager
from app.auth.models import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserResponse
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.common.redis_client import get_async_redis
from config.settings import get_settings
from database.session import get_db

router = APIRouter()
settings = get_settings()
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
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        manager = AuthManager(db)
        await manager.logout(body.refresh_token)

        # Blacklist the access token
        access_token = credentials.credentials
        try:
            payload = jwt.decode(access_token, settings.JWT_SECRET, algorithms=["HS256"])
            exp = payload.get("exp", 0)
            ttl = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
            if ttl > 0:
                token_hash = hashlib.sha256(access_token.encode()).hexdigest()
                redis = get_async_redis()
                await redis.setex(f"auth:blacklist:{token_hash}", ttl, "1")
        except Exception:
            pass  # best-effort blacklist

        return ok(message="Logged out")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)


@router.get("/me", response_model=None)
async def me(current_user: User = Depends(get_current_user)) -> JSONResponse:
    return ok(UserResponse.model_validate(current_user).model_dump(mode="json"))
