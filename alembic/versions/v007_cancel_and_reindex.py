"""Add cancelled status and celery_task_id for cancel/reindex support

Revision ID: v007
Revises: v006
Create Date: 2026-05-29
"""
from alembic import op

revision = "v007"
down_revision = "v006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE kbprocessingstatusenum ADD VALUE IF NOT EXISTS 'cancelled'")
    op.execute("ALTER TYPE wccrawlstatusenum ADD VALUE IF NOT EXISTS 'cancelled'")
    op.execute("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR")
    op.execute("ALTER TABLE website_urls ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR")


def downgrade() -> None:
    pass  # PostgreSQL does not support removing enum values; columns left in place
