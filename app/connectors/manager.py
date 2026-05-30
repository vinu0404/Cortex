import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.connectors.db_models import ConnectorDefinition, ConnectorInstance, ConnectorStatusEnum
from app.connectors.encryption import decrypt_json, encrypt_json
from app.common.exceptions import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

_CONNECTOR_DEFINITIONS = [
    {
        "slug": "gmail",
        "display_name": "Gmail",
        "auth_type": "oauth2",
        "icon": "📧",
        "tools": [
            {"name": "gmail_read_mail", "description": "Read emails from Gmail inbox", "requires_hitl": False},
            {"name": "gmail_send_mail", "description": "Send an email via Gmail", "requires_hitl": True},
            {"name": "gmail_create_draft", "description": "Create a draft email in Gmail", "requires_hitl": False},
            {"name": "gmail_list_labels", "description": "List Gmail labels", "requires_hitl": False},
        ],
    },
    {
        "slug": "github",
        "display_name": "GitHub",
        "auth_type": "oauth2",
        "icon": "🐙",
        "tools": [
            {"name": "github_list_repos", "description": "List GitHub repositories", "requires_hitl": False},
            {"name": "github_list_issues", "description": "List open issues", "requires_hitl": False},
            {"name": "github_create_issue", "description": "Create a GitHub issue", "requires_hitl": True},
            {"name": "github_list_pull_requests", "description": "Get pull requests", "requires_hitl": False},
        ],
    },
    {
        "slug": "calendar",
        "display_name": "Google Calendar",
        "auth_type": "oauth2",
        "icon": "📅",
        "tools": [
            {"name": "calendar_list_events", "description": "List upcoming calendar events", "requires_hitl": False},
            {"name": "calendar_create_event", "description": "Create a calendar event", "requires_hitl": True},
            {"name": "calendar_delete_event", "description": "Delete a calendar event", "requires_hitl": True},
        ],
    },
    {
        "slug": "salesforce",
        "display_name": "Salesforce",
        "auth_type": "oauth2",
        "icon": "☁️",
        "tools": [
            {"name": "salesforce_query", "description": "Search Salesforce records using SOQL", "requires_hitl": False},
            {"name": "salesforce_get_record", "description": "Get a specific Salesforce record", "requires_hitl": False},
            {"name": "salesforce_create_record", "description": "Create a Salesforce record", "requires_hitl": True},
            {"name": "salesforce_update_record", "description": "Update a Salesforce record", "requires_hitl": True},
        ],
    },
    {
        "slug": "tavily",
        "display_name": "Web Search (Tavily)",
        "auth_type": "apikey",
        "icon": "🔍",
        "tools": [
            {"name": "web_search", "description": "Search the web", "requires_hitl": False},
            {"name": "web_search_news", "description": "Search recent news", "requires_hitl": False},
            {"name": "fetch_url", "description": "Extract content from a URL", "requires_hitl": False},
        ],
    },
    {
        "slug": "database",
        "display_name": "Database",
        "auth_type": "credentials",
        "icon": "🗄️",
        "tools": [
            {"name": "sql_query",      "description": "Execute a read-only SELECT query",       "requires_hitl": False},
            {"name": "list_tables",    "description": "List all tables or collections",          "requires_hitl": False},
            {"name": "describe_table", "description": "Show column schema for a table",          "requires_hitl": False},
            {"name": "mongodb_query",  "description": "Run a MongoDB aggregation pipeline",      "requires_hitl": False},
        ],
    },
]


def _save_token_expiry(instance: ConnectorInstance, tokens: dict) -> None:
    expires_in = tokens.get("expires_in")
    instance.token_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        if expires_in and int(expires_in) > 0 else None
    )


class ConnectorManager:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def seed_definitions(self) -> None:
        for defn in _CONNECTOR_DEFINITIONS:
            existing = await self._db.scalar(
                select(ConnectorDefinition).where(ConnectorDefinition.slug == defn["slug"])
            )
            if not existing:
                self._db.add(ConnectorDefinition(**defn))
        try:
            await self._db.flush()
        except IntegrityError as e:
            logger.error("Connector definition seed skipped (already exists): %s", e)

    async def list_definitions(self) -> list[ConnectorDefinition]:
        result = await self._db.scalars(
            select(ConnectorDefinition).where(ConnectorDefinition.is_active.is_(True))
        )
        return list(result)

    async def list_user_instances(self, user_id: UUID) -> list[ConnectorInstance]:
        result = await self._db.scalars(
            select(ConnectorInstance)
            .where(ConnectorInstance.user_id == user_id)
            .where(ConnectorInstance.status == ConnectorStatusEnum.active)
            .options(selectinload(ConnectorInstance.definition))
        )
        return list(result)

    async def get_auth_url(self, slug: str, user_id: UUID) -> tuple[str, str]:
        import secrets
        from app.common.redis_client import get_async_redis
        defn = await self._get_definition(slug)
        if defn.auth_type.value != "oauth2":
            raise ValueError(f"Connector '{slug}' does not use OAuth2")
        connector = self._get_connector_class(slug)()
        state = f"{slug}:{user_id}:{secrets.token_urlsafe(16)}"
        redis = get_async_redis()
        await redis.setex(f"oauth_state:{state}", 600, "1")
        return connector.get_auth_url(state), state

    async def handle_callback(self, code: str, state: str) -> ConnectorInstance:
        from app.common.redis_client import get_async_redis
        redis = get_async_redis()
        if not await redis.getdel(f"oauth_state:{state}"):
            raise ValueError("Invalid or expired OAuth state")

        parts = state.split(":", 2)
        if len(parts) < 2:
            raise ValueError("Invalid OAuth state")

        slug, user_id_str = parts[0], parts[1]
        user_id = UUID(user_id_str)

        defn = await self._get_definition(slug)
        connector = self._get_connector_class(slug)()
        tokens = await connector.handle_callback(code, state)

        instance = ConnectorInstance(
            user_id=user_id,
            definition_id=defn.id,
            encrypted_tokens=encrypt_json(tokens),
            account_label=tokens.get("account_label"),
            status=ConnectorStatusEnum.active,
        )
        _save_token_expiry(instance, tokens)
        self._db.add(instance)
        try:
            await self._db.flush()
        except IntegrityError as e:
            logger.error("Failed to create connector instance: %s", e)
            raise ConflictError("Connector already connected") from e

        return instance

    async def connect_credentials(
        self,
        user_id: UUID,
        slug: str,
        connection_string: str,
        db_type: str,
        label: str | None = None,
    ) -> ConnectorInstance:
        from sqlalchemy import delete as sa_delete
        defn = await self._get_definition(slug)
        if defn.auth_type.value != "credentials":
            from app.common.exceptions import AppError
            raise AppError("CONNECTOR_NOT_CREDENTIALS", "This connector uses OAuth — use the connect flow", 400)

        tokens = {"access_token": connection_string, "db_type": db_type}
        encrypted = encrypt_json(tokens)
        account_label = label or f"{db_type} database"

        await self._db.execute(
            sa_delete(ConnectorInstance).where(
                ConnectorInstance.user_id == user_id,
                ConnectorInstance.definition_id == defn.id,
            )
        )

        instance = ConnectorInstance(
            user_id=user_id,
            definition_id=defn.id,
            encrypted_tokens=encrypted,
            account_label=account_label,
            status=ConnectorStatusEnum.active,
        )
        self._db.add(instance)
        await self._db.flush()
        return instance

    async def refresh_connector_tokens(self, instance: ConnectorInstance) -> dict:
        connector = self._get_connector_class(instance.definition.slug)()
        new_tokens = await connector.refresh_access_token(decrypt_json(instance.encrypted_tokens))
        instance.encrypted_tokens = encrypt_json(new_tokens)
        _save_token_expiry(instance, new_tokens)
        instance.updated_at = datetime.now(timezone.utc)
        await self._db.flush()
        return new_tokens

    async def delete_instance(self, instance_id: UUID, user_id: UUID) -> None:
        instance = await self._db.get(ConnectorInstance, instance_id)
        if not instance or instance.user_id != user_id:
            raise NotFoundError("ConnectorInstance", str(instance_id))
        await self._db.delete(instance)

    async def get_decrypted_tokens(self, instance_id: UUID, user_id: UUID) -> dict:
        instance = await self._db.get(ConnectorInstance, instance_id)
        if not instance or instance.user_id != user_id:
            raise NotFoundError("ConnectorInstance", str(instance_id))
        return decrypt_json(instance.encrypted_tokens)

    async def _get_definition(self, slug: str) -> ConnectorDefinition:
        defn = await self._db.scalar(
            select(ConnectorDefinition).where(ConnectorDefinition.slug == slug)
        )
        if not defn:
            raise NotFoundError("ConnectorDefinition", slug)
        return defn

    def _get_connector_class(self, slug: str):
        from connectors.gmail.connector import GmailConnector
        from connectors.github.connector import GitHubConnector
        from connectors.calendar.connector import CalendarConnector
        from connectors.salesforce.connector import SalesforceConnector
        mapping = {
            "gmail": GmailConnector,
            "github": GitHubConnector,
            "calendar": CalendarConnector,
            "salesforce": SalesforceConnector,
        }
        cls = mapping.get(slug)
        if not cls:
            raise NotFoundError("Connector", slug)
        return cls

    async def _get_connector_class_safe(self, slug: str):
        """Like _get_connector_class but returns None for credentials-type connectors."""
        defn = await self._get_definition(slug)
        if defn.auth_type.value == "credentials":
            return None
        return self._get_connector_class(slug)
