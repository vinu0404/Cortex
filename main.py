import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import litellm
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
import os

from app.auth.controller import router as auth_router
from app.workspaces.controller import router as workspaces_router
from app.agents.controller import router as agents_router
from app.connectors.controller import router as connectors_router
from app.api_keys.controller import router as api_keys_router
from app.personas.controller import router as personas_router
from app.chat.controller import router as chat_router
from app.cron_jobs.controller import router as cron_jobs_router
from app.common.health import check_database, check_redis, readiness_payload
from app.common.middleware import RequestContextMiddleware
from app.common.api_response import fail, ok
from config.settings import get_settings
from database.session import engine
from app.common.exceptions import AppError
settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # LiteLLM's built-in Langfuse integration reads LANGFUSE_HOST
    os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_BASE_URL or "")
    litellm.success_callback = ["langfuse"]
    litellm.failure_callback = ["langfuse"]
    litellm.set_verbose = False
    litellm.suppress_debug_info = True
    for _log_name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(_log_name).setLevel(logging.WARNING)

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
    return fail("VALIDATION_ERROR", "; ".join(msgs), 422, details=exc.errors())


@app.exception_handler(AppError)
async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return fail(exc.code, exc.message, exc.status_code)


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return fail("HTTP_ERROR", message, exc.status_code, details=exc.detail)


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return fail("INTERNAL_ERROR", "An unexpected error occurred. Please try again.", 500)

# ---- API Routers ----
api_prefix = f"/api/{settings.API_VERSION}"
app.include_router(auth_router, prefix=f"{api_prefix}/auth", tags=["auth"])
app.include_router(workspaces_router, prefix=f"{api_prefix}/workspaces", tags=["workspaces"])
app.include_router(agents_router, prefix=api_prefix, tags=["agents"])
app.include_router(connectors_router, prefix=f"{api_prefix}/connectors", tags=["connectors"])
app.include_router(api_keys_router, prefix=f"{api_prefix}/api-keys", tags=["api-keys"])
app.include_router(personas_router, prefix=f"{api_prefix}/personas", tags=["personas"])
app.include_router(chat_router, prefix=api_prefix, tags=["chat"])
app.include_router(cron_jobs_router, prefix=f"{api_prefix}/cron-jobs", tags=["cron-jobs"])

# ---- Admin Router ----
from app.admin.controller import router as admin_router
app.include_router(admin_router, prefix=f"{api_prefix}/admin", tags=["admin"])

from app.knowledge_bases.controller import router as knowledge_bases_router
app.include_router(knowledge_bases_router, prefix=api_prefix, tags=["knowledge-bases"])

from app.website_collections.controller import router as website_collections_router
app.include_router(website_collections_router, prefix=api_prefix, tags=["website-collections"])

from app.embed.controller import public_router as public_embed_router, router as embed_router
app.include_router(embed_router, prefix=api_prefix, tags=["embed"])
app.include_router(public_embed_router, tags=["embed"])

from app.vinu.controller import router as vinu_router
app.include_router(vinu_router, prefix=f"{api_prefix}/vinu", tags=["vinu"])

from app.mcp_servers.controller import router as mcp_servers_router
app.include_router(mcp_servers_router, prefix=f"{api_prefix}/mcp-servers", tags=["mcp-servers"])


# ---- Health & Frontend ----
@app.get("/health")
async def health() -> JSONResponse:
    return ok({"environment": settings.ENVIRONMENT}, message="Service is alive")


@app.get("/ready")
async def ready() -> JSONResponse:
    checks = [await check_database(), await check_redis()]
    payload = readiness_payload(checks)
    if payload["status"] == "ok":
        return ok(payload, message="Service is ready")
    return fail("NOT_READY", "Service dependencies are not ready", 503, details=payload)


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


@app.get("/workspace-dashboard.html")
async def serve_workspace_dashboard():
    return FileResponse("frontend/workspace-dashboard.html")

@app.get("/cron-jobs.html")
async def serve_cron_jobs():
    return FileResponse("frontend/cron-jobs.html")


@app.get("/mcp-servers.html")
async def serve_mcp_servers():
    return FileResponse("frontend/mcp-servers.html")
