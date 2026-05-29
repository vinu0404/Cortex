import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import litellm
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

    from app.common.langfuse_client import get_langfuse
    get_langfuse().flush()
    await engine.dispose()
    logger.info("Cortex shutdown")


app = FastAPI(
    title="Cortex",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_dev else None,
    redoc_url=None,
)

cors_origins = ["*"] if settings.is_dev else settings.CORS_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    msgs = []
    for e in exc.errors():
        loc = " → ".join(str(l) for l in e["loc"] if l != "body")
        msgs.append(f"{loc}: {e['msg']}" if loc else e["msg"])
    return JSONResponse(
        status_code=422,
        content={"status": "error", "code": "VALIDATION_ERROR", "message": "; ".join(msgs)},
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "code": "INTERNAL_ERROR", "message": "An unexpected error occurred. Please try again."},
    )

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

from app.knowledge_bases.controller import router as knowledge_bases_router
app.include_router(knowledge_bases_router, tags=["knowledge-bases"])

from app.website_collections.controller import router as website_collections_router
app.include_router(website_collections_router, tags=["website-collections"])


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


@app.get("/admin.html")
async def serve_admin():
    return FileResponse("frontend/admin.html")


@app.get("/knowledge-bases.html")
async def serve_knowledge_bases():
    return FileResponse("frontend/knowledge-bases.html")


@app.get("/website-collections.html")
async def serve_website_collections():
    return FileResponse("frontend/website-collections.html")
