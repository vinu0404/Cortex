"""Add credentials value to authtypeenum

Revision ID: v010
Revises: v009
Create Date: 2026-05-30
"""
from alembic import op

revision = "v010"
down_revision = "v009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE authtypeenum ADD VALUE IF NOT EXISTS 'credentials'")


def downgrade() -> None:
    pass  # PostgreSQL cannot remove enum values; safe to leave
