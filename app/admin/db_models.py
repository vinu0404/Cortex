from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AdminUserUpdateRequest
from app.agents.db_models import Agent
from app.api_keys.db_models import UserApiKey
from app.auth.db_models import RefreshToken, User
from app.chat.db_models import (
    Conversation,
    ConversationSummary,
    HitlRequest,
    Message,
    MessageArtifact,
    UserLongTermMemory,
)
from app.common.pagination import build_cursor_page
from app.connectors.db_models import ConnectorDefinition, ConnectorInstance
from app.knowledge_bases.db_models import AgentKnowledgeBase, KbDocument, KnowledgeBase
from app.personas.db_models import AgentPersona, Persona
from app.website_collections.db_models import AgentWebsiteCollection, WebsiteCollection, WebsiteUrl
from app.workspaces.db_models import Workspace


class AdminModelService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_stats(self) -> dict:
        user_count = await self._count(User)
        workspace_count = await self._db.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.deleted_at.is_(None))
        )
        return {
            "total_users": user_count,
            "total_workspaces": workspace_count or 0,
            "total_conversations": await self._count(Conversation),
            "total_messages": await self._count(Message),
        }

    async def get_user(self, user_id: UUID) -> User | None:
        return await self._db.get(User, user_id)

    async def update_user(self, user: User, body: AdminUserUpdateRequest) -> User:
        if body.is_active is not None:
            user.is_active = body.is_active
        if body.role is not None:
            user.role = body.role
        await self._db.commit()
        await self._db.refresh(user)
        return user

    async def list_table(self, table: str, cursor_created_at, cursor_id, limit: int) -> dict:
        spec = _TABLES[table]
        query = _cursor_query(spec["model"], cursor_created_at, cursor_id, limit)
        if spec.get("active_only"):
            query = query.where(spec["model"].deleted_at.is_(None))
        items = list(await self._db.scalars(query))
        page = build_cursor_page(items, limit)
        return {
            "items": [spec["row"](item) for item in page.items],
            "next_cursor": page.next_cursor,
            "has_next": page.has_next,
        }

    async def list_junction(self, table: str, limit: int) -> dict:
        spec = _JUNCTION_TABLES[table]
        rows = list(await self._db.scalars(select(spec["model"]).limit(limit)))
        return {"items": [spec["row"](row) for row in rows]}

    async def _count(self, model) -> int:
        return await self._db.scalar(select(func.count()).select_from(model)) or 0


def _cursor_query(model, cursor_created_at, cursor_id, limit):
    query = select(model).order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    if cursor_created_at and cursor_id:
        query = query.where(
            (model.created_at < cursor_created_at)
            | ((model.created_at == cursor_created_at) & (model.id < cursor_id))
        )
    return query


def _user_dict(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


_TABLES = {
    "users": {"model": User, "row": _user_dict},
    "workspaces": {
        "model": Workspace,
        "active_only": True,
        "row": lambda w: {
            "id": str(w.id),
            "user_id": str(w.user_id),
            "name": w.name,
            "created_at": w.created_at.isoformat(),
        },
    },
    "conversations": {
        "model": Conversation,
        "row": lambda c: {
            "id": str(c.id),
            "user_id": str(c.user_id),
            "workspace_id": str(c.workspace_id),
            "title": c.title,
            "created_at": c.created_at.isoformat(),
        },
    },
    "agents": {
        "model": Agent,
        "row": lambda a: {
            "id": str(a.id),
            "workspace_id": str(a.workspace_id),
            "user_id": str(a.user_id),
            "name": a.name,
            "agent_type": a.agent_type.value,
            "model_id": a.model_id,
            "deleted_at": a.deleted_at.isoformat() if a.deleted_at else None,
            "created_at": a.created_at.isoformat(),
        },
    },
    "personas": {
        "model": Persona,
        "row": lambda p: {
            "id": str(p.id),
            "user_id": str(p.user_id),
            "name": p.name,
            "created_at": p.created_at.isoformat(),
        },
    },
    "messages": {
        "model": Message,
        "row": lambda m: {
            "id": str(m.id),
            "conversation_id": str(m.conversation_id),
            "role": m.role.value,
            "content": m.content[:100],
            "total_cost_usd": m.total_cost_usd,
            "latency_ms": m.latency_ms,
            "created_at": m.created_at.isoformat(),
        },
    },
    "conversation-summaries": {
        "model": ConversationSummary,
        "row": lambda s: {
            "id": str(s.id),
            "conversation_id": str(s.conversation_id),
            "message_range_start": s.message_range_start,
            "message_range_end": s.message_range_end,
            "created_at": s.created_at.isoformat(),
        },
    },
    "hitl-requests": {
        "model": HitlRequest,
        "row": lambda h: {
            "id": str(h.id),
            "conversation_id": str(h.conversation_id),
            "agent_id": h.agent_id,
            "tool_names": h.tool_names,
            "status": h.status.value,
            "expires_at": h.expires_at.isoformat(),
            "created_at": h.created_at.isoformat(),
        },
    },
    "message-artifacts": {
        "model": MessageArtifact,
        "row": lambda a: {
            "id": str(a.id),
            "message_id": str(a.message_id),
            "conversation_id": str(a.conversation_id),
            "user_id": str(a.user_id),
            "type": a.type,
            "title": a.title,
            "filename": a.filename,
            "created_at": a.created_at.isoformat(),
        },
    },
    "knowledge-bases": {
        "model": KnowledgeBase,
        "row": lambda kb: {
            "id": str(kb.id),
            "user_id": str(kb.user_id),
            "name": kb.name,
            "document_count": kb.document_count,
            "created_at": kb.created_at.isoformat(),
        },
    },
    "kb-documents": {
        "model": KbDocument,
        "row": lambda d: {
            "id": str(d.id),
            "kb_id": str(d.kb_id),
            "user_id": str(d.user_id),
            "filename": d.filename,
            "processing_status": d.processing_status.value,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat(),
        },
    },
    "website-collections": {
        "model": WebsiteCollection,
        "row": lambda wc: {
            "id": str(wc.id),
            "user_id": str(wc.user_id),
            "name": wc.name,
            "url_count": wc.url_count,
            "created_at": wc.created_at.isoformat(),
        },
    },
    "website-urls": {
        "model": WebsiteUrl,
        "row": lambda wu: {
            "id": str(wu.id),
            "collection_id": str(wu.collection_id),
            "user_id": str(wu.user_id),
            "url": wu.url,
            "crawl_status": wu.crawl_status.value,
            "created_at": wu.created_at.isoformat(),
        },
    },
    "connector-definitions": {
        "model": ConnectorDefinition,
        "row": lambda d: {
            "id": str(d.id),
            "slug": d.slug,
            "display_name": d.display_name,
            "auth_type": d.auth_type.value,
            "created_at": d.created_at.isoformat(),
        },
    },
    "connector-instances": {
        "model": ConnectorInstance,
        "row": lambda i: {
            "id": str(i.id),
            "user_id": str(i.user_id),
            "definition_id": str(i.definition_id),
            "status": i.status.value,
            "account_label": i.account_label,
            "created_at": i.created_at.isoformat(),
        },
    },
    "api-keys": {
        "model": UserApiKey,
        "row": lambda k: {
            "id": str(k.id),
            "user_id": str(k.user_id),
            "name": k.name,
            "provider": k.provider,
            "created_at": k.created_at.isoformat(),
        },
    },
    "long-term-memory": {
        "model": UserLongTermMemory,
        "row": lambda m: {
            "id": str(m.id),
            "user_id": str(m.user_id),
            "created_at": m.created_at.isoformat(),
        },
    },
    "refresh-tokens": {
        "model": RefreshToken,
        "row": lambda t: {
            "id": str(t.id),
            "user_id": str(t.user_id),
            "expires_at": t.expires_at.isoformat(),
            "revoked_at": t.revoked_at.isoformat() if t.revoked_at else None,
            "created_at": t.created_at.isoformat(),
        },
    },
}

_JUNCTION_TABLES = {
    "agent-knowledge-bases": {
        "model": AgentKnowledgeBase,
        "row": lambda row: {"agent_id": str(row.agent_id), "kb_id": str(row.kb_id)},
    },
    "agent-website-collections": {
        "model": AgentWebsiteCollection,
        "row": lambda row: {"agent_id": str(row.agent_id), "collection_id": str(row.collection_id)},
    },
    "agent-personas": {
        "model": AgentPersona,
        "row": lambda row: {"agent_id": str(row.agent_id), "persona_id": str(row.persona_id)},
    },
}
