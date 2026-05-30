"""Add content column to message_artifacts for inline artifact storage

Revision ID: v008
Revises: v007
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa

revision = "v008"
down_revision = "v007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message_artifacts", sa.Column("content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_artifacts", "content")
