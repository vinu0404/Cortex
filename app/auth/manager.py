import hashlib
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db_models import RefreshToken, RoleEnum, User
from app.auth.models import TokenResponse
from app.common.exceptions import ConflictError, UnauthorizedError
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
        self._db = db

    async def register(self, email: str, password: str) -> TokenResponse:
        existing = await self._db.scalar(select(User).where(User.email == email))
        if existing:
            raise ConflictError("Email already registered")

        user = User(email=email, hashed_password=_hash_password(password))
        self._db.add(user)
        await self._db.flush()
        return await self._issue_tokens(user)

    async def login(self, email: str, password: str) -> TokenResponse:
        user = await self._db.scalar(select(User).where(User.email == email))
        if not user or not _verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password")
        if not user.is_active:
            raise UnauthorizedError("Account is inactive")
        if _ph.check_needs_rehash(user.hashed_password):
            user.hashed_password = _hash_password(password)
        return await self._issue_tokens(user)

    async def refresh(self, raw_token: str) -> TokenResponse:
        token_hash = _hash_token(raw_token)
        now = datetime.now(timezone.utc)

        record = await self._db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .where(RefreshToken.revoked_at.is_(None))
            .where(RefreshToken.expires_at > now)
        )
        if not record:
            raise UnauthorizedError("Invalid or expired refresh token")

        record.revoked_at = now
        await self._db.flush()

        user = await self._db.get(User, record.user_id)
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or inactive")

        return await self._issue_tokens(user)

    async def logout(self, raw_token: str, user_id: UUID) -> None:
        token_hash = _hash_token(raw_token)
        record = await self._db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .where(RefreshToken.revoked_at.is_(None))
        )
        if record and record.user_id == user_id:
            record.revoked_at = datetime.now(timezone.utc)

    async def get_user_by_id(self, user_id: UUID) -> User | None:
        return await self._db.get(User, user_id)

    async def _issue_tokens(self, user: User) -> TokenResponse:
        raw_refresh = _create_refresh_token_raw()
        expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

        token_record = RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(raw_refresh),
            expires_at=expires,
        )
        self._db.add(token_record)

        return TokenResponse(
            access_token=_create_access_token(user.id, user.role),
            refresh_token=raw_refresh,
        )
