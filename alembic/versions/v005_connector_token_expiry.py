"""Add token_expires_at to connector_instances

Revision ID: v005
Revises: v004
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "v005"
down_revision = "v004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connector_instances",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connector_instances", "token_expires_at")
