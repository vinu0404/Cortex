import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.db_models import User
from app.auth.dependencies import get_current_user
from app.common.api_response import fail, ok
from app.common.exceptions import AppError
from app.mcp_servers.manager import MCPServerManager
from app.mcp_servers.models import MCPServerCreate, MCPServerResponse, MCPServerUpdate, MCPToolHITLUpdate
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("", response_model=None)
async def list_servers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        servers = await MCPServerManager(db).list_servers(current_user.id)
        return ok([MCPServerResponse.model_validate(s).model_dump(mode="json") for s in servers])
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("list_servers failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to list MCP servers", 500)


@router.post("", response_model=None)
async def create_server(
    body: MCPServerCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        server = await MCPServerManager(db).create_server(current_user.id, body)
        return ok(MCPServerResponse.model_validate(server).model_dump(mode="json"), status_code=201)
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("create_server failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to create MCP server", 500)


@router.get("/{server_id}", response_model=None)
async def get_server(
    server_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        server = await MCPServerManager(db).get_server(server_id, current_user.id)
        return ok(MCPServerResponse.model_validate(server).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("get_server failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to get MCP server", 500)


@router.put("/{server_id}", response_model=None)
async def update_server(
    server_id: UUID,
    body: MCPServerUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        server = await MCPServerManager(db).update_server(server_id, current_user.id, body)
        return ok(MCPServerResponse.model_validate(server).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("update_server failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to update MCP server", 500)


@router.delete("/{server_id}", response_model=None)
async def delete_server(
    server_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        await MCPServerManager(db).delete_server(server_id, current_user.id)
        return ok(message="MCP server deleted")
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("delete_server failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to delete MCP server", 500)


@router.post("/{server_id}/sync", response_model=None)
async def sync_server(
    server_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        server = await MCPServerManager(db).sync_tools(server_id, current_user.id)
        return ok(MCPServerResponse.model_validate(server).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("sync_server failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to sync MCP server tools", 500)


@router.patch("/{server_id}/tools/{tool_name}/hitl", response_model=None)
async def update_tool_hitl(
    server_id: UUID,
    tool_name: str,
    body: MCPToolHITLUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        server = await MCPServerManager(db).update_tool_hitl(server_id, current_user.id, tool_name, body.requires_hitl)
        return ok(MCPServerResponse.model_validate(server).model_dump(mode="json"))
    except AppError as e:
        return fail(e.code, e.message, e.status_code)
    except Exception as e:
        logger.error("update_tool_hitl failed: %s", e, exc_info=True)
        return fail("INTERNAL_ERROR", "Failed to update tool HITL", 500)
