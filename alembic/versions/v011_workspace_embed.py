"""Add embed fields to workspaces

Revision ID: v011
Revises: v010
Create Date: 2026-05-30
"""
import sqlalchemy as sa
from alembic import op

revision = "v011"
down_revision = "v010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("embed_token", sa.String(64), nullable=True, unique=True, index=True))
    op.add_column("workspaces", sa.Column("embed_enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("workspaces", sa.Column("embed_hitl_auto_approve", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("workspaces", sa.Column("embed_disable_on_budget", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("workspaces", "embed_disable_on_budget")
    op.drop_column("workspaces", "embed_hitl_auto_approve")
    op.drop_column("workspaces", "embed_enabled")
    op.drop_column("workspaces", "embed_token")
