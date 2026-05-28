import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import litellm
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langfuse.callback import CallbackHandler

from app.common.middleware import RequestContextMiddleware
from config.settings import get_settings
from database.session import engine

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Wire LiteLLM → Langfuse auto-tracing
    langfuse_handler = CallbackHandler(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_BASE_URL,
    )
    litellm.callbacks = [langfuse_handler]
    litellm.set_verbose = settings.is_dev

    # Seed connector definitions
    from app.connectors.manager import ConnectorManager
    from database.session import get_custom_db_context_session
    async with get_custom_db_context_session() as db:
        await ConnectorManager(db).seed_definitions()

    logger.info("Cortex app started [%s]", settings.ENVIRONMENT)
    yield

    await engine.dispose()
    logger.info("Cortex app shutdown")


app = FastAPI(
    title="Cortex",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_dev else None,
    redoc_url=None,
)

# CORS
cors_origins = ["*"] if settings.is_dev else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)

# Static files
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}


# Routers registered after modules are built:
# from app.auth.controller import router as auth_router
# from app.workspaces.controller import router as workspaces_router
# from app.agents.controller import router as agents_router
# from app.connectors.controller import router as connectors_router
# from app.api_keys.controller import router as api_keys_router
# from app.personas.controller import router as personas_router
# from app.chat.controller import router as chat_router
# app.include_router(auth_router, prefix="/auth", tags=["auth"])
# app.include_router(workspaces_router, prefix="/workspaces", tags=["workspaces"])
# app.include_router(agents_router, tags=["agents"])
# app.include_router(connectors_router, prefix="/connectors", tags=["connectors"])
# app.include_router(api_keys_router, prefix="/api-keys", tags=["api-keys"])
# app.include_router(personas_router, prefix="/personas", tags=["personas"])
# app.include_router(chat_router, tags=["chat"])
