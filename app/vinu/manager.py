import json
import logging
from uuid import UUID

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.manager import ApiKeyManager
from app.auth.db_models import User
from app.common.exceptions import AppError
from app.common.guardrails import BLOCKED_INPUT_MESSAGE, SAFE_RESPONSE_FALLBACK, check_input, check_output
from app.common.langfuse_client import get_compiled_prompt
from app.common.retry import acompletion_with_retry
from app.connectors.encryption import decrypt_str
from app.knowledge_bases.manager import KnowledgeBaseManager
from app.vinu.db_models import VinuModelService
from app.vinu.models import VinuChatRequest, VinuChatResult
from app.website_collections.manager import WebsiteCollectionManager
from config.settings import get_settings
from core.schemas import MemoryCompressionOutput
from core.title_generator import generate_title
from database.session import get_custom_db_context_session
from tools.registry import get_registry

settings = get_settings()
logger = logging.getLogger(__name__)


class VinuManager:
    def __init__(self, db: AsyncSession):
        self._vinu_model_service = VinuModelService(db) if db is not None else None

    async def update_agent_name(self, user_id: UUID, name: str | None) -> None:
        await self._vinu_model_service.update_agent_name(user_id, name)

    async def list_conversations(
        self,
        user_id: UUID,
        limit: int = 20,
        cursor_created_at=None,
        cursor_id=None,
    ):
        return await self._vinu_model_service.list_conversations(
            user_id, limit, cursor_created_at, cursor_id
        )

    async def delete_conversation(self, conv_id: UUID, user_id: UUID) -> None:
        await self._vinu_model_service.delete_conversation(conv_id, user_id)

    async def get_conversation(self, conv_id: UUID, user_id: UUID):
        return await self._vinu_model_service.get_conversation(conv_id, user_id)

    async def list_messages_paginated(self, conv_id: UUID, limit: int, cursor_created_at=None, cursor_id=None):
        return await self._vinu_model_service.list_messages_paginated(
            conv_id, limit, cursor_created_at, cursor_id
        )

    async def save_build(self, conv_id: UUID, build_result: dict) -> None:
        await self._vinu_model_service.save_build(conv_id, build_result)

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

    async def compress(self, messages: list[dict], model_id: str, api_key: str | None):
        non_system = [m for m in messages if m["role"] != "system"]
        to_compress = non_system[:settings.VINU_COMPRESS_FIRST_N]
        keep = non_system[settings.VINU_COMPRESS_FIRST_N:]
        messages_text = "\n".join(f"{m['role'].upper()}: {m['content'][:2000]}" for m in to_compress)
        prompt_text = get_compiled_prompt("memory_compression", {"messages": messages_text})
        try:
            resp = await acompletion_with_retry(
                model=model_id,
                messages=[{"role": "user", "content": prompt_text}],
                response_format={"type": "json_object"},
                api_key=api_key,
                metadata={"trace_name": "vinu_memory_compression"},
            )
            result = MemoryCompressionOutput.model_validate_json(resp.choices[0].message.content)
            summary = result.summary
            return summary, [{"role": "system", "content": f"[Summary] {summary}"}] + keep
        except Exception:
            logger.exception("Vinu memory compression failed")
            return None

    async def prepare_chat_response(self, body: VinuChatRequest, user: User) -> VinuChatResult:
        async with get_custom_db_context_session() as db:
            db_service = VinuModelService(db)
            if body.conversation_id is None:
                conv = await db_service.create_conversation(user.id)
                conv_id = conv.id
                history = []
                is_new = True
            else:
                conv_id = body.conversation_id
                await db_service.get_conversation(conv_id, user.id)
                history = await db_service.load_messages(conv_id)
                is_new = False

        was_compressed = False
        async with get_custom_db_context_session() as db:
            keys = await ApiKeyManager(db).list_keys(user.id)
            model_id, api_key = _pick_model_and_key(keys)
            user_context = await _build_user_context(user, keys, db)
            if self.needs_compression(history):
                compressed = await self.compress(history, model_id, api_key)
                if compressed is not None:
                    summary, history = compressed
                    await VinuModelService(db).save_summary(
                        conv_id, summary, 0, settings.VINU_COMPRESS_FIRST_N
                    )
                    was_compressed = True

        if settings.GUARDRAILS_ENABLED:
            guard = await check_input(body.message, api_key)
            if guard.blocked:
                logger.warning("Vinu input guardrail blocked (category=%s)", guard.category)
                raise AppError("GUARDRAIL_BLOCKED", BLOCKED_INPUT_MESSAGE, 400)

        system_prompt = _assemble_system_prompt(user, user_context)
        reply, phase, plan, questions = await _call_llm(
            history, system_prompt, body.message, model_id, api_key
        )

        if settings.GUARDRAILS_ENABLED:
            out_guard = await check_output(reply, api_key)
            if out_guard.blocked:
                logger.warning("Vinu output guardrail blocked (category=%s)", out_guard.category)
                reply = SAFE_RESPONSE_FALLBACK

        new_title = None
        if is_new and reply:
            try:
                new_title = await generate_title(body.message, reply, "gpt-4o-mini", api_key)
            except Exception:
                logger.exception("Vinu title generation failed")

        async with get_custom_db_context_session() as db:
            db_service = VinuModelService(db)
            await db_service.append_messages(conv_id, [
                {"role": "user", "content": body.message},
                {"role": "assistant", "content": reply},
            ])
            if new_title:
                await db_service.update_name(conv_id, new_title)
            if plan:
                await db_service.save_plan(conv_id, plan)

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
    mcp_servers = await VinuModelService(db).list_active_mcp_servers(user.id)
    if mcp_servers:
        lines.append("\nMCP servers connected (tools available to agents):")
        for srv in mcp_servers:
            tool_names = [t["name"] for t in (srv.discovered_tools or [])]
            tool_list = ", ".join(tool_names) if tool_names else "no tools yet"
            lines.append(f'  - {srv.name} ({srv.server_url}): {tool_list}')
            lines.append(f'    Use connector_slug: mcp:{srv.id}')
    return "\n".join(lines)


def _assemble_system_prompt(user: User, user_context: str) -> str:
    registry = get_registry()
    tool_schemas = registry.get_tool_schemas(registry.all_tool_names())
    tools_context = "\n".join(
        f"- {t['name']} (connector: {t['connector']}): {t['description']}" for t in tool_schemas
    ) or "No tools registered."
    return get_compiled_prompt(
        "vinu_system_prompt",
        {"agent_name": user.vinu_agent_name or "Vinu", "tools_context": tools_context, "user_context": user_context},
    )


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
    resp = await acompletion_with_retry(
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
    return parsed.get("reply", ""), parsed.get("phase", "gathering"), parsed.get("plan"), parsed.get("questions")
