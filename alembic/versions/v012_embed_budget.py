"""Add per-workspace embed budget/spend tracking, drop embed_disable_on_budget

Revision ID: v012
Revises: v011
Create Date: 2026-05-31
"""
import sqlalchemy as sa
from alembic import op

revision = "v012"
down_revision = "v011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("workspaces", "embed_disable_on_budget")
    op.add_column("workspaces", sa.Column("embed_budget_usd", sa.Float(), nullable=True))
    op.add_column("workspaces", sa.Column("embed_budget_tokens", sa.BigInteger(), nullable=True))
    op.add_column("workspaces", sa.Column("embed_spend_usd", sa.Float(), nullable=False, server_default="0"))
    op.add_column("workspaces", sa.Column("embed_spend_tokens", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("workspaces", "embed_spend_tokens")
    op.drop_column("workspaces", "embed_spend_usd")
    op.drop_column("workspaces", "embed_budget_tokens")
    op.drop_column("workspaces", "embed_budget_usd")
    op.add_column("workspaces", sa.Column("embed_disable_on_budget", sa.Boolean(), nullable=False, server_default="false"))
