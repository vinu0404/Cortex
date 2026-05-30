"""Add sources column to messages

Revision ID: v009
Revises: v008
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa

revision = "v009"
down_revision = "v008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("sources", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "sources")
