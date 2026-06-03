"""Add plan_runs and agent_run_records tables

Revision ID: v020
Revises: v019
Create Date: 2026-06-02
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "v020"
down_revision = "v019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("user_query", sa.Text, nullable=False),
        sa.Column("master_reasoning", sa.Text, nullable=False, server_default=""),
        sa.Column("plan", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_runs_conversation_id", "plan_runs", ["conversation_id"])

    op.create_table(
        "agent_run_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_run_id", UUID(as_uuid=True), sa.ForeignKey("plan_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("agent_name", sa.String(128), nullable=False),
        sa.Column("retry_attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_task", sa.Text, nullable=False, server_default=""),
        sa.Column("input_tools", JSONB, nullable=False, server_default="[]"),
        sa.Column("input_dependency_outputs", JSONB, nullable=False, server_default="{}"),
        sa.Column("output_data", JSONB, nullable=True),
        sa.Column("output_error", sa.Text, nullable=True),
        sa.Column("task_done", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("tokens_input", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("time_taken_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_run_records_plan_run_id", "agent_run_records", ["plan_run_id"])
    op.create_index("ix_agent_run_records_conversation_id", "agent_run_records", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("agent_run_records")
    op.drop_table("plan_runs")
