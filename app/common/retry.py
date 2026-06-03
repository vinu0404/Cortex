import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    wait_fixed,
)

from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_HTTP_RETRIABLE_STATUS = {429, 500, 502, 503, 504}
_NON_RETRIABLE_QDRANT_STATUS = {400, 401, 403, 404}


def _is_retriable_litellm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is not None and status_code not in _HTTP_RETRIABLE_STATUS:
        return False
    retriable_names = {
        "APIConnectionError",
        "APIError",
        "InternalServerError",
        "RateLimitError",
        "ServiceUnavailableError",
        "Timeout",
    }
    return type(exc).__name__ in retriable_names or status_code in _HTTP_RETRIABLE_STATUS


def _is_retriable_http_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _HTTP_RETRIABLE_STATUS
    return False


def _is_retriable_qdrant_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in _NON_RETRIABLE_QDRANT_STATUS:
        return False
    return True


@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,
        max=settings.LLM_RETRY_WAIT_MAX,
        jitter=settings.LLM_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable_litellm_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def acompletion_with_retry(**kwargs: Any) -> Any:
    return await litellm.acompletion(**kwargs)


@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,
        max=settings.LLM_RETRY_WAIT_MAX,
        jitter=settings.LLM_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable_litellm_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def aembedding_with_retry(**kwargs: Any) -> Any:
    return await litellm.aembedding(**kwargs)


@retry(
    stop=stop_after_attempt(settings.HTTP_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.HTTP_RETRY_WAIT_MIN,
        max=settings.HTTP_RETRY_WAIT_MAX,
        jitter=settings.HTTP_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable_http_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def httpx_get_with_retry(url: str, **kwargs: Any) -> httpx.Response:
    response = httpx.get(url, **kwargs)
    response.raise_for_status()
    return response


@retry(
    stop=stop_after_attempt(settings.HTTP_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.HTTP_RETRY_WAIT_MIN,
        max=settings.HTTP_RETRY_WAIT_MAX,
        jitter=settings.HTTP_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable_http_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def async_http_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response


@retry(
    stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),
    wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def async_redis_call(redis_client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(redis_client, method_name)
    return await method(*args, **kwargs)


@retry(
    stop=stop_after_attempt(settings.HTTP_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.HTTP_RETRY_WAIT_MIN,
        max=settings.HTTP_RETRY_WAIT_MAX,
        jitter=settings.HTTP_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable_qdrant_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def async_qdrant_call(operation: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    return await operation(*args, **kwargs)
