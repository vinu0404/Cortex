import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import litellm
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langfuse.callback import CallbackHandler

from app.auth.controller import router as auth_router
from app.workspaces.controller import router as workspaces_router
from app.agents.controller import router as agents_router
from app.connectors.controller import router as connectors_router
from app.api_keys.controller import router as api_keys_router
from app.personas.controller import router as personas_router
from app.chat.controller import router as chat_router
from app.common.middleware import RequestContextMiddleware
from config.settings import get_settings
from database.session import engine

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    langfuse_handler = CallbackHandler(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_BASE_URL,
    )
    litellm.callbacks = [langfuse_handler]
    litellm.set_verbose = settings.is_dev

    from app.connectors.manager import ConnectorManager
    from database.session import get_custom_db_context_session
    async with get_custom_db_context_session() as db:
        await ConnectorManager(db).seed_definitions()

    from tools.registry import get_registry
    get_registry().auto_discover()

    logger.info("Cortex started [%s]", settings.ENVIRONMENT)
    yield

    await engine.dispose()
    logger.info("Cortex shutdown")


app = FastAPI(
    title="Cortex",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_dev else None,
    redoc_url=None,
)

cors_origins = ["*"] if settings.is_dev else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# ---- API Routers ----
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(workspaces_router, prefix="/workspaces", tags=["workspaces"])
app.include_router(agents_router, tags=["agents"])
app.include_router(connectors_router, prefix="/connectors", tags=["connectors"])
app.include_router(api_keys_router, prefix="/api-keys", tags=["api-keys"])
app.include_router(personas_router, prefix="/personas", tags=["personas"])
app.include_router(chat_router, tags=["chat"])

# ---- Admin Router ----
from app.admin.controller import router as admin_router
app.include_router(admin_router, prefix="/admin", tags=["admin"])


# ---- Health & Frontend ----
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@app.get("/auth.html")
async def serve_auth():
    return FileResponse("frontend/auth.html")


@app.get("/index.html")
@app.get("/")
async def serve_index():
    return FileResponse("frontend/index.html")


@app.get("/workspace.html")
async def serve_workspace():
    return FileResponse("frontend/workspace.html")


@app.get("/chat.html")
async def serve_chat():
    return FileResponse("frontend/chat.html")


@app.get("/dashboard.html")
async def serve_dashboard():
    return FileResponse("frontend/dashboard.html")
