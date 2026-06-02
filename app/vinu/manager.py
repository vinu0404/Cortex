import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import litellm
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.manager import ApiKeyManager
from app.auth.db_models import User
from app.common.exceptions import ForbiddenError, NotFoundError
from app.common.langfuse_client import get_compiled_prompt
from app.connectors.encryption import decrypt_str
from app.knowledge_bases.manager import KnowledgeBaseManager
from app.vinu.db_models import VinuConversation, VinuMessage, VinuMessageRoleEnum, VinuSummary
from app.vinu.models import VinuChatRequest, VinuChatResult
from app.website_collections.manager import WebsiteCollectionManager
from config.settings import get_settings
from core.schemas import MemoryCompressionOutput
from core.title_generator import generate_title
from database.session import get_custom_db_context_session
from tools.registry import get_registry

settings = get_settings()
logger = logging.getLogger(__name__)


class VinuConversationManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_conversations(
        self,
        user_id: UUID,
        limit: int = 20,
        cursor_created_at: datetime | None = None,
        cursor_id: UUID | None = None,
    ) -> list[VinuConversation]:
        query = (
            select(VinuConversation)
            .where(VinuConversation.user_id == user_id)
            .order_by(VinuConversation.created_at.desc(), VinuConversation.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (VinuConversation.created_at < cursor_created_at)
                | (
                    (VinuConversation.created_at == cursor_created_at)
                    & (VinuConversation.id < cursor_id)
                )
            )
        result = await self._db.scalars(query)
        return list(result)

    async def create_conversation(self, user_id: UUID, name: str = "New Chat") -> VinuConversation:
        conv = VinuConversation(user_id=user_id, name=name)
        self._db.add(conv)
        await self._db.flush()
        return conv

    async def get_conversation(self, conv_id: UUID, user_id: UUID) -> VinuConversation:
        conv = await self._db.get(VinuConversation, conv_id)
        if not conv:
            raise NotFoundError("VinuConversation", str(conv_id))
        if conv.user_id != user_id:
            raise ForbiddenError("Access denied")
        return conv

    async def delete_conversation(self, conv_id: UUID, user_id: UUID) -> None:
        conv = await self.get_conversation(conv_id, user_id)
        await self._db.delete(conv)

    async def load_messages(self, conv_id: UUID) -> list[dict]:
        summary_row = await self._db.scalar(
            select(VinuSummary)
            .where(VinuSummary.conversation_id == conv_id)
            .order_by(VinuSummary.created_at.desc())
            .limit(1)
        )
        rows = list(await self._db.scalars(
            select(VinuMessage)
            .where(VinuMessage.conversation_id == conv_id)
            .order_by(VinuMessage.created_at.asc())
        ))
        messages: list[dict] = [{"role": r.role.value, "content": r.content} for r in rows]
        if summary_row:
            messages = [
                {"role": "system", "content": f"[Summary of earlier conversation] {summary_row.summary}"}
            ] + messages
        return messages

    async def list_messages_paginated(
        self,
        conv_id: UUID,
        limit: int,
        cursor_created_at: datetime | None = None,
        cursor_id: UUID | None = None,
    ) -> list[VinuMessage]:
        query = (
            select(VinuMessage)
            .where(VinuMessage.conversation_id == conv_id)
            .order_by(VinuMessage.created_at.desc(), VinuMessage.id.desc())
            .limit(limit + 1)
        )
        if cursor_created_at and cursor_id:
            query = query.where(
                (VinuMessage.created_at < cursor_created_at)
                | (
                    (VinuMessage.created_at == cursor_created_at)
                    & (VinuMessage.id < cursor_id)
                )
            )
        result = await self._db.scalars(query)
        return list(result)

    async def append_messages(self, conv_id: UUID, messages: list[dict]) -> None:
        for m in messages:
            self._db.add(VinuMessage(
                conversation_id=conv_id,
                role=VinuMessageRoleEnum(m["role"]),
                content=m["content"],
            ))
        await self._db.execute(
            update(VinuConversation)
            .where(VinuConversation.id == conv_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        await self._db.flush()

    async def save_summary(
        self, conv_id: UUID, summary: str, range_start: int, range_end: int
    ) -> None:
        await self._db.execute(
            delete(VinuSummary).where(VinuSummary.conversation_id == conv_id)
        )
        self._db.add(VinuSummary(
            conversation_id=conv_id,
            summary=summary,
            message_range_start=range_start,
            message_range_end=range_end,
        ))
        await self._db.flush()

    async def update_agent_name(self, user_id: UUID, name: str | None) -> None:
        await self._db.execute(
            update(User).where(User.id == user_id).values(vinu_agent_name=name)
        )
        await self._db.flush()

    async def update_name(self, conv_id: UUID, name: str) -> None:
        await self._db.execute(
            update(VinuConversation)
            .where(VinuConversation.id == conv_id)
            .values(name=name)
        )
        await self._db.flush()

    async def save_plan(self, conv_id: UUID, plan: dict) -> None:
        await self._db.execute(
            update(VinuConversation)
            .where(VinuConversation.id == conv_id)
            .values(last_plan=plan)
        )
        await self._db.flush()

    async def save_build(self, conv_id: UUID, build_result: dict) -> None:
        await self._db.execute(
            update(VinuConversation)
            .where(VinuConversation.id == conv_id)
            .values(last_build=build_result)
        )
        await self._db.flush()

    def needs_compression(self, messages: list[dict]) -> bool:
        non_system = sum(1 for m in messages if m["role"] != "system")
        if non_system > settings.VINU_MEMORY_WINDOW:
            return True
        try:
            tokens = litellm.token_counter(model="gpt-4o", messages=messages)
            return tokens > settings.VINU_COMPRESS_TOKEN_THRESHOLD
        except Exception:
            logger.warning("Vinu token counting failed, skipping threshold check", exc_info=True)
            return False

    async def compress(
        self, messages: list[dict], model_id: str, api_key: str | None
    ) -> tuple[str, list[dict]] | None:
        """Returns (summary, new_messages) or None if compression fails."""
        non_system = [m for m in messages if m["role"] != "system"]
        to_compress = non_system[:settings.VINU_COMPRESS_FIRST_N]
        keep = non_system[settings.VINU_COMPRESS_FIRST_N:]
        messages_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:2000]}" for m in to_compress
        )
        prompt_text = get_compiled_prompt("memory_compression", {"messages": messages_text})
        try:
            resp = await litellm.acompletion(
                model=model_id,
                messages=[{"role": "user", "content": prompt_text}],
                response_format={"type": "json_object"},
                api_key=api_key,
                metadata={"trace_name": "vinu_memory_compression"},
            )
            result = MemoryCompressionOutput.model_validate_json(resp.choices[0].message.content)
            summary = result.summary
            new_messages = (
                [{"role": "system", "content": f"[Summary] {summary}"}]
                + keep
            )
            return summary, new_messages
        except Exception:
            logger.exception("Vinu memory compression failed")
            return None


# ---------------------------------------------------------------------------
# Chat orchestration — module-level; manages its own DB sessions so the
# controller stays HTTP-only and holds no long-lived connections.
# ---------------------------------------------------------------------------

async def prepare_chat_response(body: VinuChatRequest, user: User) -> VinuChatResult:
    # Session A: create/get conversation + load history
    async with get_custom_db_context_session() as db:
        mgr = VinuConversationManager(db)
        if body.conversation_id is None:
            conv = await mgr.create_conversation(user.id)
            conv_id = conv.id
            history: list[dict] = []
            is_new = True
        else:
            conv_id = body.conversation_id
            await mgr.get_conversation(conv_id, user.id)
            history = await mgr.load_messages(conv_id)
            is_new = False

    # user context building, and optional compression — eliminates duplicate query.
    was_compressed = False
    async with get_custom_db_context_session() as db:
        keys = await ApiKeyManager(db).list_keys(user.id)
        model_id, api_key = _pick_model_and_key(keys)
        user_context = await _build_user_context(user, keys, db)
        mgr = VinuConversationManager(db)
        if mgr.needs_compression(history):
            result = await mgr.compress(history, model_id, api_key)
            if result is not None:
                summary, history = result
                await mgr.save_summary(conv_id, summary, 0, settings.VINU_COMPRESS_FIRST_N)
                was_compressed = True

    # LLM call — no DB session held during network I/O
    if settings.GUARDRAILS_ENABLED:
        from app.common.exceptions import AppError
        from app.common.guardrails import BLOCKED_INPUT_MESSAGE, check_input
        guard = await check_input(body.message, api_key)
        if guard.blocked:
            logger.warning("Vinu input guardrail blocked (category=%s)", guard.category)
            raise AppError("GUARDRAIL_BLOCKED", BLOCKED_INPUT_MESSAGE, 400)

    system_prompt = _assemble_system_prompt(user, user_context)
    reply, phase, plan, questions = await _call_llm(
        history, system_prompt, body.message, model_id, api_key
    )

    if settings.GUARDRAILS_ENABLED:
        from app.common.guardrails import SAFE_RESPONSE_FALLBACK, check_output
        out_guard = await check_output(reply, api_key)
        if out_guard.blocked:
            logger.warning("Vinu output guardrail blocked (category=%s)", out_guard.category)
            reply = SAFE_RESPONSE_FALLBACK

    # Title generation
    new_title: str | None = None
    if is_new and reply:
        try:
            new_title = await generate_title(body.message, reply, "gpt-4o-mini", api_key)
        except Exception:
            logger.exception("Vinu title generation failed")

    #persist turn + title + plan
    async with get_custom_db_context_session() as db:
        mgr = VinuConversationManager(db)
        await mgr.append_messages(conv_id, [
            {"role": "user", "content": body.message},
            {"role": "assistant", "content": reply},
        ])
        if new_title:
            await mgr.update_name(conv_id, new_title)
        if plan:
            await mgr.save_plan(conv_id, plan)

    return VinuChatResult(
        conv_id=conv_id,
        is_new=is_new,
        reply=reply,
        phase=phase,
        plan=plan,
        questions=questions,
        new_title=new_title,
        was_compressed=was_compressed,
    )


def _pick_model_and_key(keys: list) -> tuple[str, str | None]:
    for key in keys:
        if key.available_models:
            return key.available_models[0], decrypt_str(key.encrypted_key)
    return settings.DEFAULT_MODEL, None


async def _build_user_context(user: User, keys: list, db: AsyncSession) -> str:
    lines: list[str] = []
    available_models: list[str] = []
    for key in keys:
        if key.available_models:
            available_models.extend(key.available_models)

    if available_models:
        lines.append("Available models from your API keys:")
        lines.append(", ".join(available_models[:10]))
    else:
        lines.append("No API keys configured — agents will use the platform default model.")

    kbs = await KnowledgeBaseManager(db).list_kbs(user.id)
    if kbs:
        lines.append("\nExisting Knowledge Bases (attach directly by name):")
        for kb in kbs:
            lines.append(f'- "{kb.name}" ({kb.document_count or 0} documents)')

    wcs = await WebsiteCollectionManager(db).list_collections(user.id)
    if wcs:
        lines.append("\nExisting Website Collections (attach directly by name):")
        for wc in wcs:
            lines.append(f'- "{wc.name}" ({wc.url_count} URLs)')

    return "\n".join(lines)


def _assemble_system_prompt(user: User, user_context: str) -> str:
    registry = get_registry()
    tool_schemas = registry.get_tool_schemas(registry.all_tool_names())
    tools_context = "\n".join(
        f"- {t['name']} (connector: {t['connector']}): {t['description']}"
        for t in tool_schemas
    ) or "No tools registered."
    return get_compiled_prompt("vinu_system_prompt", {
        "agent_name": user.vinu_agent_name or "Vinu",
        "tools_context": tools_context,
        "user_context": user_context,
    })


async def _call_llm(
    history: list[dict],
    system_prompt: str,
    user_message: str,
    model_id: str,
    api_key: str | None,
) -> tuple[str, str, dict | None, list | None]:
    messages = [{"role": "system", "content": system_prompt}]
    messages += [m for m in history if m["role"] != "system"]
    messages.append({"role": "user", "content": user_message})

    resp = await litellm.acompletion(
        model=model_id,
        messages=messages,
        response_format={"type": "json_object"},
        api_key=api_key,
        metadata={"trace_name": "vinu_chat"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"reply": raw, "phase": "gathering"}

    return (
        parsed.get("reply", ""),
        parsed.get("phase", "gathering"),
        parsed.get("plan"),
        parsed.get("questions"),
    )
