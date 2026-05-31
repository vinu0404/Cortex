"""vinu_conversations: add last_plan column

Revision ID: v014
Revises: v013_vinu_tables
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = "v014"
down_revision = "v013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vinu_conversations", sa.Column("last_plan", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("vinu_conversations", "last_plan")
