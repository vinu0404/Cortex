import logging
from functools import lru_cache

import redis as redis_sync
import redis.asyncio as redis_async

from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_sync_redis() -> redis_sync.Redis:
    return redis_sync.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
    )


@lru_cache(maxsize=1)
def get_async_redis() -> redis_async.Redis:
    return redis_async.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
