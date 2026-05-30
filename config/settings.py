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
    SHORT_TERM_COMPRESS_TOKEN_THRESHOLD: int = 80_000
    COMPOSER_AGENT_OUTPUT_MAX_CHARS: int = 8_000

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

    # --- Tavily ---
    TAVILY_API_KEY: str = ""

    # --- Backblaze B2 (S3-compatible storage) ---
    B2_ENDPOINT: str = ""
    B2_REGION: str = "us-east-005"
    B2_ACCESS_KEY_ID: str = ""
    B2_SECRET_ACCESS_KEY: str = ""
    B2_BUCKET: str = ""
    B2_PRESIGN_EXPIRY: int = 300  # seconds

    # --- Vision ---
    VISION_MODEL: str = "gpt-4o"

    # --- Knowledge Base: Embeddings ---
    KB_EMBEDDING_MODEL: str = "text-embedding-3-small"
    KB_EMBEDDING_DIMS: int = 1536
    KB_EMBED_BATCH_SIZE: int = 96

    # --- Knowledge Base: Chunking ---
    KB_CHUNK_SIZE: int = 1000
    KB_CHUNK_OVERLAP: int = 200
    KB_CSV_ROWS_PER_CHUNK: int = 100

    # --- Knowledge Base: Retrieval ---
    KB_TOP_K_DENSE: int = 50
    KB_TOP_K_SPARSE: int = 50
    KB_TOP_K_RRF: int = 20
    KB_TOP_K_FINAL: int = 5
    KB_RRF_K: int = 60
    KB_RERANK_STRATEGY: str = "llm"  # cross_encoder | llm | none

    # --- Knowledge Base: File limits ---
    KB_SUPPORTED_EXTENSIONS: list[str] = [
        ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv",
        ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    ]
    KB_MAX_FILES_PER_UPLOAD: int = 50
    KB_MAX_FILE_SIZE_MB: int = 100

    # --- Website Collections ---
    WC_MAX_URLS_PER_COLLECTION: int = 50
    WC_MAX_DEPTH:               int = 5
    WC_CRAWL_TIMEOUT_SECONDS:   int = 600
    WC_MAX_PAGES_PER_URL:       int = 500
    WC_CONCURRENT_REQUESTS:     int = 8
    WC_DOWNLOAD_TIMEOUT:        int = 30
    WC_OBEY_ROBOTS:             bool = True
    WC_USER_AGENT:              str = "CortexBot/1.0"
    WC_TOP_K_DENSE:             int = 50
    WC_TOP_K_FINAL:             int = 5

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
        return "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
