"""Add stdio transport columns to mcp_servers

Revision ID: v019
Revises: v018
Create Date: 2026-06-02
"""
import sqlalchemy as sa
from alembic import op

revision = "v019"
down_revision = "v018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("transport_type", sa.String(16), nullable=False, server_default="http"))
    op.add_column("mcp_servers", sa.Column("command", sa.Text, nullable=True))
    op.add_column("mcp_servers", sa.Column("encrypted_env_vars", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "encrypted_env_vars")
    op.drop_column("mcp_servers", "command")
    op.drop_column("mcp_servers", "transport_type")
