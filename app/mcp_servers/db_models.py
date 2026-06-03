import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    server_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none", server_default="none")
    auth_header_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    encrypted_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transport_type: Mapped[str] = mapped_column(String(16), nullable=False, default="http", server_default="http")
    command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_env_vars: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovered_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class MCPServerModelService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_servers(self, user_id: uuid.UUID) -> list[MCPServer]:
        rows = await self._db.scalars(
            select(MCPServer).where(MCPServer.user_id == user_id).order_by(MCPServer.created_at.asc())
        )
        return list(rows)

    async def get_server(self, server_id: uuid.UUID) -> MCPServer | None:
        return await self._db.get(MCPServer, server_id)

    async def create_server(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        transport_type: str,
        server_url: str,
        auth_type: str,
        auth_header_name: str | None,
        encrypted_token: str | None,
        command: str | None,
        encrypted_env_vars: str | None,
    ) -> MCPServer:
        server = MCPServer(
            user_id=user_id,
            name=name,
            transport_type=transport_type,
            server_url=server_url,
            auth_type=auth_type,
            auth_header_name=auth_header_name,
            encrypted_token=encrypted_token,
            command=command,
            encrypted_env_vars=encrypted_env_vars,
        )
        self._db.add(server)
        await self._db.flush()
        return server

    async def update_server_fields(self, server: MCPServer, **kwargs) -> MCPServer:
        for field, value in kwargs.items():
            if value is not None:
                setattr(server, field, value)
        await self._db.flush()
        return server

    async def delete_server(self, server: MCPServer) -> None:
        await self._db.delete(server)

    async def update_tool_hitl(self, server: MCPServer, tool_name: str, requires_hitl: bool) -> MCPServer:
        tools = list(server.discovered_tools or [])
        for tool in tools:
            if tool.get("name") == tool_name:
                tool["requires_hitl"] = requires_hitl
                break
        else:
            raise KeyError(tool_name)
        server.discovered_tools = tools
        await self._db.flush()
        return server

    async def update_discovered_tools(self, server: MCPServer, tools: list[dict], synced_at: datetime) -> MCPServer:
        server.discovered_tools = tools
        server.last_synced_at = synced_at
        await self._db.flush()
        return server
