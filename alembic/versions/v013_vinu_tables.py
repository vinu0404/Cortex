"""Add vinu tables and vinu_agent_name column on users

Revision ID: v013
Revises: v012
Create Date: 2026-05-31
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v013"
down_revision = "v012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("vinu_agent_name", sa.String(), nullable=True))

    op.create_table(
        "vinu_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default="New Chat"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_vinu_conversations_user_id", "vinu_conversations", ["user_id"])
    op.create_index("ix_vinu_conversations_created_at", "vinu_conversations", ["created_at"])

    op.create_table(
        "vinu_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vinu_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_vinu_messages_conv_created", "vinu_messages", ["conversation_id", "created_at"])

    op.create_table(
        "vinu_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vinu_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("message_range_start", sa.Integer(), nullable=False),
        sa.Column("message_range_end", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_vinu_summaries_conversation_id", "vinu_summaries", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("vinu_summaries")
    op.drop_table("vinu_messages")
    op.drop_table("vinu_conversations")
    op.drop_column("users", "vinu_agent_name")
