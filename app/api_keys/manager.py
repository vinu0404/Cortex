import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.db_models import ApiKeyModelService, UserApiKey
from app.connectors.encryption import decrypt_str, encrypt_str
from app.common.exceptions import NotFoundError
from app.common.retry import httpx_get_with_retry

logger = logging.getLogger(__name__)

_PROVIDER_PREFIXES = {
    "sk-ant-": "anthropic",
    "sk-": "openai",
    "AIza": "gemini",
    "gsk_": "groq",
    "AP": "mistral",
}

# Models that only work with v1/responses (not v1/chat/completions)
_RESPONSES_ONLY_MODELS: frozenset[str] = frozenset({
    "computer-use-preview",
    "gpt-5-codex",
    "o1-pro",
    "codex-mini-latest",
})

# Substrings that identify non-chat models (embeddings, audio, image, legacy)
_NON_CHAT_PATTERNS = (
    "embed",
    "whisper",
    "tts",
    "dall-e",
    "davinci",
    "babbage",
    "ada",
    "search",
    "similarity",
    "moderation",
)

# LiteLLM provider prefix for get_model_info lookup (openai models need no prefix)
_LITELLM_PROVIDER_PREFIX: dict[str, str] = {
    "anthropic": "anthropic/",
    "gemini": "gemini/",
    "groq": "groq/",
    "mistral": "mistral/",
    "openai": "",
}


def _is_agent_capable(model_id: str, provider: str) -> bool:
    """Return True if model supports v1/chat/completions + function calling."""
    if model_id in _RESPONSES_ONLY_MODELS:
        return False
    model_lower = model_id.lower()
    if any(p in model_lower for p in _NON_CHAT_PATTERNS):
        return False
    try:
        import litellm as _ll
        prefix = _LITELLM_PROVIDER_PREFIX.get(provider, "")
        info = _ll.get_model_info(f"{prefix}{model_id}")
        if info.get("supports_function_calling") is False:
            return False
    except Exception:
        pass  # Unknown model — allow; user knows their key
    return True


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
    try:
        raw: list[str] = []

        if provider == "openai":
            resp = httpx_get_with_retry(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {raw_key}"},
                timeout=10,
            )
            raw = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "anthropic":
            resp = httpx_get_with_retry(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": raw_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
            raw = [m["id"] for m in resp.json().get("data", [])]

        elif provider == "gemini":
            resp = httpx_get_with_retry(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": raw_key},
                timeout=10,
            )
            raw = [
                m["name"].split("/")[-1]
                for m in resp.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]

        elif provider == "groq":
            resp = httpx_get_with_retry(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {raw_key}"},
                timeout=10,
            )
            raw = [m["id"] for m in resp.json().get("data", [])]

        return [m for m in raw if _is_agent_capable(m, provider)]

    except Exception:
        logger.exception("Model discovery failed for provider %s", provider)

    return []


class ApiKeyManager:
    def __init__(self, db: AsyncSession):
        self._api_key_model_service = ApiKeyModelService(db)

    async def create_key(self, user_id: UUID, key_name: str, raw_key: str) -> UserApiKey:
        provider = _detect_provider(raw_key)
        loop = asyncio.get_running_loop()
        models = await loop.run_in_executor(None, _discover_models_sync, provider, raw_key)

        return await self._api_key_model_service.create_key(
            user_id=user_id,
            key_name=key_name,
            encrypted_key=encrypt_str(raw_key),
            provider=provider,
            available_models=models,
        )

    async def list_keys(self, user_id: UUID) -> list[UserApiKey]:
        return await self._api_key_model_service.list_keys(user_id)

    async def get_models(self, key_id: UUID, user_id: UUID) -> list[str]:
        key_record = await self._get_key(key_id, user_id)
        return [m for m in (key_record.available_models or []) if _is_agent_capable(m, key_record.provider)]

    async def refresh_models(self, key_id: UUID, user_id: UUID) -> list[str]:
        key_record = await self._get_key(key_id, user_id)
        raw_key = decrypt_str(key_record.encrypted_key)
        loop = asyncio.get_running_loop()
        models = await loop.run_in_executor(None, _discover_models_sync, key_record.provider, raw_key)
        return await self._api_key_model_service.update_models(key_record, models)

    async def delete_key(self, key_id: UUID, user_id: UUID) -> None:
        key_record = await self._get_key(key_id, user_id)
        await self._api_key_model_service.delete_key(key_record)

    async def get_decrypted_key(self, key_id: UUID, user_id: UUID) -> str:
        key_record = await self._get_key(key_id, user_id)
        return decrypt_str(key_record.encrypted_key)

    async def _get_key(self, key_id: UUID, user_id: UUID) -> UserApiKey:
        record = await self._api_key_model_service.get_key(key_id)
        if not record or record.user_id != user_id:
            raise NotFoundError("ApiKey", str(key_id))
        return record
