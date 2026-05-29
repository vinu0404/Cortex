"""Initial schema

Revision ID: v001
Revises:
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "v001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- Enums ----------------------------------------------------------------
    postgresql.ENUM("user", "admin", name="roleenum", create_type=False).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("MASTER", "CUSTOM", "COMPOSER", name="agenttypeenum", create_type=False).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("oauth2", "apikey", name="authtypeenum", create_type=False).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("active", "expired", "revoked", name="connectorstatusenum", create_type=False).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("user", "assistant", "system", name="messagerolenewnum", create_type=False).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("pending", "approved", "denied", "timed_out", name="hitlstatusenum", create_type=False).create(op.get_bind(), checkfirst=True)

    # ---- users ----------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("role", postgresql.ENUM("user", "admin", name="roleenum", create_type=False), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ---- refresh_tokens -------------------------------------------------------
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    # ---- connector_definitions ------------------------------------------------
    op.create_table(
        "connector_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("auth_type", postgresql.ENUM("oauth2", "apikey", name="authtypeenum", create_type=False), nullable=False),
        sa.Column("tools", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_connector_definitions_slug", "connector_definitions", ["slug"], unique=True)

    # ---- user_api_keys --------------------------------------------------------
    op.create_table(
        "user_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_name", sa.String(), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("available_models", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_api_keys_user_id", "user_api_keys", ["user_id"])

    # ---- workspaces -----------------------------------------------------------
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workspaces_user_id", "workspaces", ["user_id"])
    op.create_index("ix_workspaces_created_at", "workspaces", ["created_at"])

    # ---- connector_instances --------------------------------------------------
    op.create_table(
        "connector_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("connector_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("encrypted_tokens", sa.Text(), nullable=False),
        sa.Column("account_label", sa.String(), nullable=True),
        sa.Column("status", postgresql.ENUM("active", "expired", "revoked", name="connectorstatusenum", create_type=False), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "definition_id", name="uq_connector_user_definition"),
    )
    op.create_index("ix_connector_instances_user_id", "connector_instances", ["user_id"])

    # ---- agents ---------------------------------------------------------------
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("agent_type", postgresql.ENUM("MASTER", "CUSTOM", "COMPOSER", name="agenttypeenum", create_type=False), nullable=False, server_default="CUSTOM"),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user_api_keys.id", ondelete="SET NULL"), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_editable", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("tools_config", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agents_workspace_deleted", "agents", ["workspace_id", "deleted_at"])
    op.create_index("ix_agents_user_id", "agents", ["user_id"])
    # Partial unique index: one agent name per workspace (ignoring soft-deleted)
    op.create_index(
        "uq_agent_workspace_name",
        "agents",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ---- personas -------------------------------------------------------------
    op.create_table(
        "personas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_personas_user_id", "personas", ["user_id"])

    # ---- agent_personas -------------------------------------------------------
    op.create_table(
        "agent_personas",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("persona_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("personas.id", ondelete="CASCADE"), primary_key=True),
    )

    # ---- conversations --------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_workspace_id", "conversations", ["workspace_id"])
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_workspace_user_created", "conversations", ["workspace_id", "user_id", "created_at"])

    # ---- messages -------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", postgresql.ENUM("user", "assistant", "system", name="messagerolenewnum", create_type=False), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_details", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("total_cost_usd", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("langfuse_trace_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    # ---- conversation_summaries -----------------------------------------------
    op.create_table(
        "conversation_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("message_range_start", sa.Integer(), nullable=False),
        sa.Column("message_range_end", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversation_summaries_conversation_id", "conversation_summaries", ["conversation_id"])

    # ---- hitl_requests --------------------------------------------------------
    op.create_table(
        "hitl_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("tool_names", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("status", postgresql.ENUM("pending", "approved", "denied", "timed_out", name="hitlstatusenum", create_type=False), nullable=False, server_default="pending"),
        sa.Column("user_instructions", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hitl_conversation_status", "hitl_requests", ["conversation_id", "status"])

    # ---- user_long_term_memory ------------------------------------------------
    op.create_table(
        "user_long_term_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("critical_facts", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("preferences", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_user_long_term_memory_user_id", "user_long_term_memory", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_table("user_long_term_memory")
    op.drop_table("hitl_requests")
    op.drop_table("conversation_summaries")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("agent_personas")
    op.drop_table("personas")
    op.drop_table("agents")
    op.drop_table("connector_instances")
    op.drop_table("workspaces")
    op.drop_table("user_api_keys")
    op.drop_table("connector_definitions")
    op.drop_table("refresh_tokens")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS hitlstatusenum")
    op.execute("DROP TYPE IF EXISTS messagerolenewnum")
    op.execute("DROP TYPE IF EXISTS connectorstatusenum")
    op.execute("DROP TYPE IF EXISTS authtypeenum")
    op.execute("DROP TYPE IF EXISTS agenttypeenum")
    op.execute("DROP TYPE IF EXISTS roleenum")
