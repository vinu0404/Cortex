"""Message artifacts table

Revision ID: v004
Revises: v003
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "v004"
down_revision = "v003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_message_artifacts_message_id", "message_artifacts", ["message_id"])
    op.create_index("ix_message_artifacts_conversation_id", "message_artifacts", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_message_artifacts_conversation_id", table_name="message_artifacts")
    op.drop_index("ix_message_artifacts_message_id", table_name="message_artifacts")
    op.drop_table("message_artifacts")
