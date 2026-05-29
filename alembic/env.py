import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import get_settings
from database.session import Base

# Import ALL ORM models so Alembic can detect them
from app.auth.db_models import User, RefreshToken  # noqa: F401
from app.workspaces.db_models import Workspace  # noqa: F401
from app.agents.db_models import Agent  # noqa: F401
from app.connectors.db_models import ConnectorDefinition, ConnectorInstance  # noqa: F401
from app.api_keys.db_models import UserApiKey  # noqa: F401
from app.personas.db_models import Persona, AgentPersona  # noqa: F401
from app.chat.db_models import (  # noqa: F401
    Conversation, Message, ConversationSummary, HitlRequest, UserLongTermMemory
)

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(settings.DATABASE_URL, echo=False)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
