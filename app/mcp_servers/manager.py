import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.common.exceptions import AppError, MCPSubprocessError, NotFoundError
from app.connectors.encryption import decrypt_str, encrypt_str
from app.mcp_servers.db_models import MCPServer
from app.mcp_servers.models import MCPServerCreate, MCPServerUpdate
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

MCP_HTTP_TIMEOUT = 15.0


class MCPServerError(Exception):
    pass


class MCPServerManager:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_servers(self, user_id: UUID) -> list[MCPServer]:
        rows = await self._db.scalars(
            select(MCPServer).where(MCPServer.user_id == user_id).order_by(MCPServer.created_at.asc())
        )
        return list(rows)

    async def get_server(self, server_id: UUID, user_id: UUID) -> MCPServer:
        server = await self._db.get(MCPServer, server_id)
        if not server:
            raise NotFoundError("MCPServer", str(server_id))
        if server.user_id != user_id:
            raise AppError("FORBIDDEN", "Access denied", 403)
        return server

    async def create_server(self, user_id: UUID, payload: MCPServerCreate) -> MCPServer:
        encrypted_token = encrypt_str(payload.token) if payload.token else None
        encrypted_env = encrypt_str(json.dumps(payload.env_vars)) if payload.env_vars else None
        server = MCPServer(
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
        self._db.add(server)
        await self._db.flush()
        return await self._sync_tools_inline(server)

    async def update_server(self, server_id: UUID, user_id: UUID, payload: MCPServerUpdate) -> MCPServer:
        server = await self.get_server(server_id, user_id)
        if payload.name is not None:
            server.name = payload.name
        if payload.transport_type is not None:
            server.transport_type = payload.transport_type
        if payload.server_url is not None:
            server.server_url = payload.server_url
        if payload.auth_type is not None:
            server.auth_type = payload.auth_type
        if payload.auth_header_name is not None:
            server.auth_header_name = payload.auth_header_name
        if payload.token is not None:
            server.encrypted_token = encrypt_str(payload.token)
        if payload.command is not None:
            server.command = payload.command
        if payload.env_vars is not None:
            server.encrypted_env_vars = encrypt_str(json.dumps(payload.env_vars))
        await self._db.flush()
        return await self._sync_tools_inline(server)

    async def delete_server(self, server_id: UUID, user_id: UUID) -> None:
        server = await self.get_server(server_id, user_id)
        await self._db.delete(server)

    async def sync_tools(self, server_id: UUID, user_id: UUID) -> MCPServer:
        server = await self.get_server(server_id, user_id)
        return await self._sync_tools_inline(server)

    async def update_tool_hitl(self, server_id: UUID, user_id: UUID, tool_name: str, requires_hitl: bool) -> MCPServer:
        server = await self.get_server(server_id, user_id)
        tools = list(server.discovered_tools or [])
        for t in tools:
            if t.get("name") == tool_name:
                t["requires_hitl"] = requires_hitl
                break
        else:
            raise AppError("NOT_FOUND", f"Tool '{tool_name}' not found", 404)
        server.discovered_tools = tools
        await self._db.flush()
        return server

    async def _sync_tools_inline(self, server: MCPServer) -> MCPServer:
        try:
            tools = await _discover_tools(server)
        except asyncio.TimeoutError:
            raise AppError("MCP_TIMEOUT", "MCP server timed out during tool discovery", 504)
        except Exception as exc:
            logger.warning("MCP tool discovery failed for %s: %s", server.name, exc)
            raise AppError("MCP_UNREACHABLE", f"Could not reach MCP server: {exc}", 502)
        existing_hitl: dict[str, bool] = {
            t["name"]: t.get("requires_hitl", False)
            for t in (server.discovered_tools or [])
        }
        for t in tools:
            t["requires_hitl"] = existing_hitl.get(t["name"], False)
        server.discovered_tools = tools
        server.last_synced_at = datetime.now(timezone.utc)
        await self._db.flush()
        return server


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
        raise MCPServerError("stdio server has no command configured")
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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
)
async def _call_mcp_rpc(
    server_url: str,
    auth_type: str,
    auth_header_name: str | None,
    token: str | None,
    method: str,
    params: dict,
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key" and auth_header_name:
            headers[auth_header_name] = token

    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    async with httpx.AsyncClient(timeout=MCP_HTTP_TIMEOUT) as client:
        resp = await client.post(server_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        err = data["error"]
        raise MCPServerError(f"MCP error {err.get('code')}: {err.get('message')}")
    return data.get("result", {})
