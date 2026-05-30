import asyncio
import logging
from uuid import UUID

import litellm

from app.common.langfuse_client import get_compiled_prompt
from config.settings import get_settings
from core.schemas import LongTermMemory, LongTermMemoryExtraction, MemoryCompressionOutput

settings = get_settings()
logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, conversation_id: UUID):
        self.conversation_id = conversation_id
        self._messages: list[dict] = []
        self._summaries: list[str] = []

    def load(self, summaries: list[str], recent_messages: list[dict]) -> None:
        if summaries:
            self._summaries = summaries
            self._messages = [{"role": "system", "content": s} for s in summaries]
        self._messages.extend(recent_messages)

    def add_message(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})

    def get_window(self) -> list[dict]:
        return self._messages[-settings.SHORT_TERM_MEMORY_WINDOW:]

    def needs_compression(self) -> bool:
        non_system = sum(1 for m in self._messages if m["role"] != "system")
        return non_system > settings.SHORT_TERM_MEMORY_WINDOW

    async def compress(self, model_id: str, api_key: str) -> str | None:
        if not self.needs_compression():
            return None

        to_compress = self._messages[:settings.SHORT_TERM_COMPRESS_FIRST_N]
        messages_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:300]}" for m in to_compress
        )

        prompt_text = get_compiled_prompt("memory_compression", {"messages": messages_text})
        try:
            resp = await litellm.acompletion(
                model=model_id,
                messages=[{"role": "user", "content": prompt_text}],
                response_format={"type": "json_object"},
                api_key=api_key,
                metadata={"trace_name": "memory_compression"},
            )
            result = MemoryCompressionOutput.model_validate_json(resp.choices[0].message.content)
            summary = result.summary

            self._messages = (
                [{"role": "system", "content": f"[Summary] {summary}"}]
                + self._messages[settings.SHORT_TERM_COMPRESS_FIRST_N:]
            )
            return summary
        except Exception:
            logger.exception("Memory compression failed")
            return None


def schedule_long_term_memory(
    query: str,
    response: str,
    user_id: UUID,
    model_id: str,
    api_key: str,
    db_updater,
) -> None:
    """Fire-and-forget: evaluate + persist long-term memory after each response."""
    task = asyncio.create_task(
        _evaluate_long_term(query, response, user_id, model_id, api_key, db_updater)
    )
    task.add_done_callback(_log_task_error)


def _log_task_error(task: asyncio.Task) -> None:
    if task.exception():
        logger.error("Long-term memory task failed: %s", task.exception())


async def _evaluate_long_term(
    query: str,
    response: str,
    user_id: UUID,
    model_id: str,
    api_key: str,
    db_updater,
) -> None:
    prompt_text = get_compiled_prompt("long_term_memory_extraction", {
        "query": query,
        "response": response[:600],
    })
    try:
        resp = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            response_format={"type": "json_object"},
            api_key=api_key,
            metadata={"trace_name": "long_term_memory"},
        )
        extraction = LongTermMemoryExtraction.model_validate_json(resp.choices[0].message.content)
        if extraction.should_store:
            await db_updater(user_id, extraction.critical_facts, extraction.preferences)
    except Exception:
        logger.exception("Long-term memory extraction failed")
