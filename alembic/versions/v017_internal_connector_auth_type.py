"""Add internal value to authtypeenum for built-in platform connectors

Revision ID: v017
Revises: v016
Create Date: 2026-05-31
"""
from alembic import op

revision = "v017"
down_revision = "v016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE authtypeenum ADD VALUE IF NOT EXISTS 'internal'")


def downgrade() -> None:
    pass  # PostgreSQL cannot remove enum values; safe to leave
