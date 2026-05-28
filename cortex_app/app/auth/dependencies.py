import logging
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db_models import RoleEnum, User
from app.auth.manager import AuthManager
from app.common.exceptions import ForbiddenError, UnauthorizedError
from app.common.redis_client import get_async_redis
from config.settings import get_settings
from database.session import get_db

settings = get_settings()
logger = logging.getLogger(__name__)
_bearer = HTTPBearer()


async def _validate_access_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise UnauthorizedError("Invalid or expired access token")

    if payload.get("type") != "access":
        raise UnauthorizedError("Not an access token")

    redis = get_async_redis()
    import hashlib
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if await redis.exists(f"auth:blacklist:{token_hash}"):
        raise UnauthorizedError("Token has been revoked")

    return payload


async def get_current_user(
    payload: dict = Depends(_validate_access_token),
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Token missing subject")

    manager = AuthManager(db)
    user = await manager.get_user_by_id(UUID(user_id))
    if not user or not user.is_active:
        raise UnauthorizedError("User not found or inactive")

    return user


def get_current_user_with_roles(required_roles: list[RoleEnum]):
    async def _dep(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in required_roles:
            raise ForbiddenError("Insufficient permissions")
        return current_user
    return _dep


get_current_admin = get_current_user_with_roles([RoleEnum.admin])
