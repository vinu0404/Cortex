"""Add pending_upload status to kb documents

Revision ID: v006
Revises: v005
Create Date: 2026-05-29
"""
from alembic import op

revision = "v006"
down_revision = "v005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE kbprocessingstatusenum ADD VALUE IF NOT EXISTS 'pending_upload'")


def downgrade() -> None:
    pass  # PostgreSQL does not support removing enum values
