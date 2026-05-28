from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment ---
    ENVIRONMENT: Literal["dev", "prod"] = "dev"

    # --- LLM ---
    DEFAULT_MODEL: str = "gpt-4o"

    # --- Database ---
    DATABASE_URL: str = "postgresql+asyncpg://cortex:cortex@localhost:5432/cortex"

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"

    # --- Qdrant ---
    QDRANT_URL: str = "http://localhost:6333"

    # --- Auth ---
    JWT_SECRET: str = "change-me"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # --- Encryption ---
    ENCRYPTION_KEY: str = "change-me-32-byte-base64-encoded-key"

    # --- Langfuse ---
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_BASE_URL: str = "https://cloud.langfuse.com"
    LANGFUSE_PROMPT_CACHE_TTL: int = 300

    # --- Memory ---
    SHORT_TERM_MEMORY_WINDOW: int = 10
    SHORT_TERM_COMPRESS_FIRST_N: int = 4

    # --- Features ---
    ENABLE_SUGGESTIONS: bool = True

    # --- HITL ---
    HITL_TIMEOUT_SECONDS: int = 120

    # --- Token budget ---
    TOKEN_BUDGET_ENABLED: bool = True
    USER_DAILY_TOKEN_BUDGET: int = 100_000
    USER_MONTHLY_TOKEN_BUDGET: int = 2_000_000

    # --- LLM retries ---
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_WAIT_MIN: float = 1.0
    LLM_RETRY_WAIT_MAX: float = 30.0
    LLM_RETRY_JITTER: float = 2.0

    # --- HTTP retries ---
    HTTP_MAX_RETRIES: int = 3
    HTTP_RETRY_WAIT_MIN: float = 1.0
    HTTP_RETRY_WAIT_MAX: float = 30.0
    HTTP_RETRY_JITTER: float = 1.0

    # --- Redis retries ---
    REDIS_MAX_RETRIES: int = 2
    REDIS_RETRY_WAIT_FIXED: float = 0.5

    # --- CORS ---
    CORS_ORIGINS: list[str] = []

    # --- OAuth ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/connectors/callback"

    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = "http://localhost:8000/connectors/callback"

    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    SALESFORCE_REDIRECT_URI: str = "http://localhost:8000/connectors/callback"

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT == "dev"

    @property
    def log_level(self) -> str:
        return "DEBUG" if self.is_dev else "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
