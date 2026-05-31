"""vinu_conversations: add last_build column

Revision ID: v015
Revises: v014
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = "v015"
down_revision = "v014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vinu_conversations", sa.Column("last_build", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("vinu_conversations", "last_build")
