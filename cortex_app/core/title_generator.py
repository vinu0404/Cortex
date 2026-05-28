import asyncio
import logging
from uuid import UUID

import litellm

from app.common.langfuse_client import get_compiled_prompt
from config.settings import get_settings
from core.schemas import TitleGenerationOutput

settings = get_settings()
logger = logging.getLogger(__name__)


def schedule_title_generation(
    query: str,
    response: str,
    conversation_id: UUID,
    model_id: str,
    api_key: str,
    db_updater,
) -> None:
    """Fire-and-forget title generation after first message."""
    task = asyncio.create_task(
        _generate_title(query, response, conversation_id, model_id, api_key, db_updater)
    )
    task.add_done_callback(lambda t: logger.warning("Title gen failed: %s", t.exception()) if t.exception() else None)


async def _generate_title(
    query: str,
    response: str,
    conversation_id: UUID,
    model_id: str,
    api_key: str,
    db_updater,
) -> None:
    prompt_text = get_compiled_prompt("title_generation", {
        "query": query,
        "response_preview": response[:200],
    })
    try:
        resp = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            response_format=TitleGenerationOutput,
            api_key=api_key,
            metadata={"trace_name": "title_generation"},
        )
        result = TitleGenerationOutput.model_validate_json(resp.choices[0].message.content)
        await db_updater(conversation_id, result.title)
    except Exception as e:
        logger.warning("Title generation failed for %s: %s", conversation_id, e)
