import asyncio
import json
import logging
from json import JSONDecodeError
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AppError, MCPSubprocessError, NotFoundError
from app.common.retry import async_http_request_with_retry
from app.connectors.encryption import decrypt_str, encrypt_str
from app.mcp_servers.db_models import MCPServer, MCPServerModelService
from app.mcp_servers.models import MCPServerCreate, MCPServerUpdate
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

MCP_HTTP_TIMEOUT = settings.MCP_DISCOVERY_TIMEOUT_SECONDS


class MCPServerError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class MCPServerManager:
    def __init__(self, db: AsyncSession) -> None:
        self._mcp_server_model_service = MCPServerModelService(db)

    async def list_servers(self, user_id: UUID) -> list[MCPServer]:
        return await self._mcp_server_model_service.list_servers(user_id)

    async def get_server(self, server_id: UUID, user_id: UUID) -> MCPServer:
        server = await self._mcp_server_model_service.get_server(server_id)
        if not server:
            raise NotFoundError("MCPServer", str(server_id))
        if server.user_id != user_id:
            raise AppError("FORBIDDEN", "Access denied", 403)
        return server

    async def create_server(self, user_id: UUID, payload: MCPServerCreate) -> MCPServer:
        encrypted_token = encrypt_str(payload.token) if payload.token else None
        encrypted_env = encrypt_str(json.dumps(payload.env_vars)) if payload.env_vars else None
        server = await self._mcp_server_model_service.create_server(
            user_id=user_id,
            name=payload.name,
            transport_type=payload.transport_type,
            server_url=payload.server_url or "",
            auth_type=payload.auth_type,
            auth_header_name=payload.auth_header_name,
            encrypted_token=encrypted_token,
            command=payload.command,
            encrypted_env_vars=encrypted_env,
        )
        return await self._sync_tools_inline(server)

    async def update_server(self, server_id: UUID, user_id: UUID, payload: MCPServerUpdate) -> MCPServer:
        server = await self.get_server(server_id, user_id)
        await self._mcp_server_model_service.update_server_fields(
            server,
            name=payload.name,
            transport_type=payload.transport_type,
            server_url=payload.server_url,
            auth_type=payload.auth_type,
            auth_header_name=payload.auth_header_name,
            encrypted_token=encrypt_str(payload.token) if payload.token is not None else None,
            command=payload.command,
            encrypted_env_vars=encrypt_str(json.dumps(payload.env_vars)) if payload.env_vars is not None else None,
        )
        return await self._sync_tools_inline(server)

    async def delete_server(self, server_id: UUID, user_id: UUID) -> None:
        server = await self.get_server(server_id, user_id)
        await self._mcp_server_model_service.delete_server(server)

    async def sync_tools(self, server_id: UUID, user_id: UUID) -> MCPServer:
        server = await self.get_server(server_id, user_id)
        return await self._sync_tools_inline(server)

    async def update_tool_hitl(self, server_id: UUID, user_id: UUID, tool_name: str, requires_hitl: bool) -> MCPServer:
        server = await self.get_server(server_id, user_id)
        try:
            return await self._mcp_server_model_service.update_tool_hitl(server, tool_name, requires_hitl)
        except KeyError as exc:
            raise AppError("NOT_FOUND", f"Tool '{tool_name}' not found", 404) from exc

    async def _sync_tools_inline(self, server: MCPServer) -> MCPServer:
        try:
            tools = await _discover_tools(server)
        except asyncio.TimeoutError:
            raise AppError("MCP_TIMEOUT", "MCP server timed out during tool discovery", 504)
        except MCPServerError as exc:
            logger.warning("MCP tool discovery failed for %s: %s", server.name, exc.message)
            raise AppError(exc.code, exc.message, exc.status_code) from exc
        except AppError:
            raise
        except Exception as exc:
            logger.warning("MCP tool discovery failed for %s: %s", server.name, exc)
            raise AppError("MCP_UNREACHABLE", f"Could not reach MCP server: {exc}", 502)
        existing_hitl: dict[str, bool] = {
            t["name"]: t.get("requires_hitl", False)
            for t in (server.discovered_tools or [])
        }
        for t in tools:
            t["requires_hitl"] = existing_hitl.get(t["name"], False)
        return await self._mcp_server_model_service.update_discovered_tools(
            server,
            tools,
            datetime.now(timezone.utc),
        )


async def _discover_tools(server: MCPServer) -> list[dict]:
    if server.transport_type == "stdio":
        return await _discover_tools_stdio(server)
    return await _discover_tools_http(server)


async def _discover_tools_stdio(server: MCPServer) -> list[dict]:
    import shlex
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parts = shlex.split(server.command or "")
    if not parts:
        raise MCPServerError("MCP_INVALID_CONFIG", "stdio MCP server has no command configured", 422)
    env = json.loads(decrypt_str(server.encrypted_env_vars)) if server.encrypted_env_vars else {}

    async def _run() -> list[dict]:
        params = StdioServerParameters(command=parts[0], args=parts[1:], env=env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema.model_dump() if t.inputSchema else {"type": "object", "properties": {}},
                "requires_hitl": False,
            }
            for t in result.tools
        ]

    try:
        return await asyncio.wait_for(_run(), timeout=settings.MCP_DISCOVERY_TIMEOUT_SECONDS)
    except BaseExceptionGroup as eg:
        raise MCPSubprocessError(str(eg.exceptions[0])) from eg


async def _discover_tools_http(server: MCPServer) -> list[dict]:
    token = decrypt_str(server.encrypted_token) if server.encrypted_token else None
    result = await _call_mcp_rpc(
        server.server_url, server.auth_type, server.auth_header_name, token, "tools/list", {}
    )
    raw_tools = result.get("tools", [])
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}}),
            "requires_hitl": False,
        }
        for t in raw_tools
        if t.get("name")
    ]


async def _call_mcp_rpc(
    server_url: str,
    auth_type: str,
    auth_header_name: str | None,
    token: str | None,
    method: str,
    params: dict,
) -> dict:
    if not server_url:
        raise MCPServerError("MCP_INVALID_CONFIG", "HTTP MCP server has no server_url configured", 422)

    headers = {"Content-Type": "application/json"}
    if token:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key" and auth_header_name:
            headers[auth_header_name] = token

    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        async with httpx.AsyncClient(timeout=MCP_HTTP_TIMEOUT) as client:
            resp = await async_http_request_with_retry(client, "POST", server_url, json=body, headers=headers)
            data = resp.json()
    except httpx.TimeoutException as exc:
        raise MCPServerError("MCP_TIMEOUT", "MCP server timed out", 504) from exc
    except httpx.HTTPStatusError as exc:
        raise MCPServerError(
            "MCP_HTTP_ERROR",
            f"MCP server returned HTTP {exc.response.status_code}",
            502,
        ) from exc
    except httpx.RequestError as exc:
        raise MCPServerError("MCP_UNREACHABLE", f"Could not reach MCP server: {exc}", 502) from exc
    except JSONDecodeError as exc:
        raise MCPServerError("MCP_INVALID_RESPONSE", "MCP server returned invalid JSON", 502) from exc

    if "error" in data:
        err = data["error"]
        raise MCPServerError("MCP_RPC_ERROR", f"MCP error {err.get('code')}: {err.get('message')}", 502)
    return data.get("result", {})
