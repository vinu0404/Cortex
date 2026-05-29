import logging
from datetime import date, datetime, timezone
from uuid import UUID

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from app.common.exceptions import TokenBudgetExceededError
from app.common.redis_client import get_async_redis
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class TokenBudgetService:
    def __init__(self):
        self._redis = get_async_redis()

    @retry(
        stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),
        wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def check_budget(self, user_id: UUID) -> None:
        if not settings.TOKEN_BUDGET_ENABLED:
            return

        uid = str(user_id)
        today = date.today().isoformat()
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        daily_key = f"budget:daily:{uid}:{today}"
        monthly_key = f"budget:monthly:{uid}:{month}"

        daily_used = int(await self._redis.get(daily_key) or 0)
        monthly_used = int(await self._redis.get(monthly_key) or 0)

        if daily_used >= settings.USER_DAILY_TOKEN_BUDGET:
            raise TokenBudgetExceededError("daily")
        if monthly_used >= settings.USER_MONTHLY_TOKEN_BUDGET:
            raise TokenBudgetExceededError("monthly")

    @retry(
        stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),
        wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def record_usage(self, user_id: UUID, tokens: int) -> None:
        if not settings.TOKEN_BUDGET_ENABLED or tokens <= 0:
            return

        uid = str(user_id)
        today = date.today().isoformat()
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        daily_key = f"budget:daily:{uid}:{today}"
        monthly_key = f"budget:monthly:{uid}:{month}"

        pipe = self._redis.pipeline()
        pipe.incrby(daily_key, tokens)
        pipe.expire(daily_key, 86400)
        pipe.incrby(monthly_key, tokens)
        pipe.expire(monthly_key, 86400 * 32)
        await pipe.execute()
