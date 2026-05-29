import asyncio
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.db_models import UserApiKey
from app.connectors.encryption import decrypt_str, encrypt_str
from app.common.exceptions import NotFoundError

logger = logging.getLogger(__name__)

_PROVIDER_PREFIXES = {
    "sk-ant-": "anthropic",
    "sk-": "openai",
    "AIza": "gemini",
    "gsk_": "groq",
    "AP": "mistral",
}


def _detect_provider(key: str) -> str:
    for prefix, provider in _PROVIDER_PREFIXES.items():
        if key.startswith(prefix):
            return provider
    return "unknown"


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def _discover_models_sync(provider: str, raw_key: str) -> list[str]:
    """Blocking call — must run in executor."""
    import httpx

    try:
        if provider == "openai":
            resp = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {raw_key}"},
                timeout=10,
            )
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", []) if "gpt" in m["id"] or "o1" in m["id"] or "o3" in m["id"]]

        if provider == "anthropic":
            resp = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": raw_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]

        if provider == "gemini":
            resp = httpx.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": raw_key},
                timeout=10,
            )
            resp.raise_for_status()
            return [m["name"].split("/")[-1] for m in resp.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]

        if provider == "groq":
            resp = httpx.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {raw_key}"},
                timeout=10,
            )
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]

    except Exception:
        logger.exception("Model discovery failed for provider %s", provider)

    return []


class ApiKeyManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_key(self, user_id: UUID, key_name: str, raw_key: str) -> UserApiKey:
        provider = _detect_provider(raw_key)
        loop = asyncio.get_running_loop()
        models = await loop.run_in_executor(None, _discover_models_sync, provider, raw_key)

        key_record = UserApiKey(
            user_id=user_id,
            key_name=key_name,
            encrypted_key=encrypt_str(raw_key),
            provider=provider,
            available_models=models,
        )
        self._db.add(key_record)
        await self._db.flush()
        return key_record

    async def list_keys(self, user_id: UUID) -> list[UserApiKey]:
        result = await self._db.scalars(
            select(UserApiKey).where(UserApiKey.user_id == user_id).order_by(UserApiKey.created_at.desc())
        )
        return list(result)

    async def get_models(self, key_id: UUID, user_id: UUID) -> list[str]:
        key_record = await self._get_key(key_id, user_id)
        return key_record.available_models

    async def delete_key(self, key_id: UUID, user_id: UUID) -> None:
        key_record = await self._get_key(key_id, user_id)
        await self._db.delete(key_record)

    async def get_decrypted_key(self, key_id: UUID, user_id: UUID) -> str:
        key_record = await self._get_key(key_id, user_id)
        return decrypt_str(key_record.encrypted_key)

    async def _get_key(self, key_id: UUID, user_id: UUID) -> UserApiKey:
        record = await self._db.get(UserApiKey, key_id)
        if not record or record.user_id != user_id:
            raise NotFoundError("ApiKey", str(key_id))
        return record
