from celery import Celery
from config.settings import get_settings
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from celery.signals import worker_process_init
import database.session as _db
settings = get_settings()

celery_app = Celery(
    "cortex",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_BROKER_URL,
    include=["document_pipeline.tasks", "web_pipeline.tasks", "app.cron_jobs.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    redbeat_redis_url=settings.CELERY_BROKER_URL,
)


@worker_process_init.connect
def _configure_worker(**kwargs):
    # Register all ORM models so FK references resolve in ForkPoolWorker subprocesses
    import database.models
    _engine = create_async_engine(_db.settings.DATABASE_URL, poolclass=NullPool, echo=False)
    _db.engine = _engine
    _db.AsyncSessionFactory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )
