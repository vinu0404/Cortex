import hashlib
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db_models import AuthModelService, RoleEnum, User
from app.auth.models import TokenResponse, UserUpdate
from app.common.exceptions import ConflictError, ServiceUnavailableError, UnauthorizedError
from app.common.redis_client import get_async_redis
from app.common.retry import async_redis_call
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
_ph = PasswordHasher()


def _hash_password(password: str) -> str:
    return _ph.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _create_access_token(user_id: UUID, role: RoleEnum) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role.value, "exp": expires, "type": "access"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def _create_refresh_token_raw() -> str:
    import secrets
    return secrets.token_urlsafe(64)


class AuthManager:
    def __init__(self, db: AsyncSession):
        self._auth_model_service = AuthModelService(db)

    async def register(self, email: str, password: str) -> TokenResponse:
        existing = await self._auth_model_service.find_user_by_email(email)
        if existing:
            raise ConflictError("Email already registered")

        user = await self._auth_model_service.create_user(email, _hash_password(password))
        return await self._issue_tokens(user)

    async def login(self, email: str, password: str) -> TokenResponse:
        user = await self._auth_model_service.find_user_by_email(email)
        if not user or not _verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password")
        if not user.is_active:
            raise UnauthorizedError("Account is inactive")
        if _ph.check_needs_rehash(user.hashed_password):
            await self._auth_model_service.update_user_password(user, _hash_password(password))
        return await self._issue_tokens(user)

    async def refresh(self, raw_token: str) -> TokenResponse:
        token_hash = _hash_token(raw_token)
        now = datetime.now(timezone.utc)

        record = await self._auth_model_service.get_valid_refresh_token(token_hash, now)
        if not record:
            raise UnauthorizedError("Invalid or expired refresh token")

        await self._auth_model_service.revoke_refresh_token(record, now)

        user = await self._auth_model_service.get_user_by_id(record.user_id)
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or inactive")

        return await self._issue_tokens(user)

    async def logout(self, raw_token: str, user_id: UUID, access_token: str | None = None) -> None:
        token_hash = _hash_token(raw_token)
        record = await self._auth_model_service.get_active_refresh_token(token_hash)
        if record and record.user_id == user_id:
            await self._auth_model_service.revoke_refresh_token(record, datetime.now(timezone.utc))
        if access_token:
            await self._blacklist_access_token(access_token)

    async def get_user_by_id(self, user_id: UUID) -> User | None:
        return await self._auth_model_service.get_user_by_id(user_id)

    async def update_user_profile(self, user: User, body: UserUpdate) -> User:
        await self._auth_model_service.update_user_profile(user, body.timezone, body.vinu_agent_name)
        await self._auth_model_service.save_changes()
        await self._auth_model_service.refresh(user)
        return user

    async def get_user_stats(self, user_id: UUID) -> dict:
        return await self._auth_model_service.get_user_stats(user_id)

    async def get_recent_conversations(self, user_id: UUID, limit: int) -> list[dict]:
        return await self._auth_model_service.get_recent_conversations(user_id, limit)

    async def get_dashboard_analytics(self, user_id: UUID, days: int = 15, workspace_limit: int = 6) -> dict:
        return await self._auth_model_service.get_dashboard_analytics(user_id, days, workspace_limit)

    async def _issue_tokens(self, user: User) -> TokenResponse:
        raw_refresh = _create_refresh_token_raw()
        expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

        await self._auth_model_service.create_refresh_token(
            user.id,
            _hash_token(raw_refresh),
            expires,
        )

        return TokenResponse(
            access_token=_create_access_token(user.id, user.role),
            refresh_token=raw_refresh,
        )

    async def _blacklist_access_token(self, access_token: str) -> None:
        try:
            payload = jwt.decode(access_token, settings.JWT_SECRET, algorithms=["HS256"])
        except JWTError as e:
            raise UnauthorizedError("Invalid or expired access token") from e
        exp = payload.get("exp", 0)
        ttl = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
        if ttl <= 0:
            return
        try:
            redis = get_async_redis()
            await async_redis_call(redis, "setex", f"auth:blacklist:{_hash_token(access_token)}", ttl, "1")
        except Exception as e:
            logger.error("Failed to blacklist access token during logout", exc_info=True)
            raise ServiceUnavailableError("Could not revoke access token") from e
