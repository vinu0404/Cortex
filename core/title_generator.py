import logging

import litellm

from app.common.langfuse_client import get_compiled_prompt
from config.settings import get_settings
from core.schemas import TitleGenerationOutput

settings = get_settings()
logger = logging.getLogger(__name__)


async def generate_title(
    query: str,
    response: str,
    model_id: str,
    api_key: str,
) -> str | None:
    """Returns generated title string or None on failure."""
    logger.info("Title generation using model %s", model_id)
    prompt_text = get_compiled_prompt("title_generation", {
        "query": query,
        "response_preview": response[:200],
    })
    try:
        resp = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            response_format={"type": "json_object"},
            api_key=api_key,
            metadata={"trace_name": "title_generation"},
        )
        result = TitleGenerationOutput.model_validate_json(resp.choices[0].message.content)
        logger.info("Title generated: %r", result.title)
        return result.title
    except Exception as e:
        logger.error("Title generation failed (model=%s): %s", model_id, e)
        return None
