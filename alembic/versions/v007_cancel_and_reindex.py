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
    op.execute("UPDATE kb_documents SET processing_status = 'failed' WHERE processing_status = 'cancelled'")
    op.execute("UPDATE website_urls SET crawl_status = 'failed' WHERE crawl_status = 'cancelled'")
    op.execute("ALTER TABLE kb_documents DROP COLUMN IF EXISTS celery_task_id")
    op.execute("ALTER TABLE website_urls DROP COLUMN IF EXISTS celery_task_id")

    op.execute("ALTER TABLE kb_documents ALTER COLUMN processing_status DROP DEFAULT")
    op.execute("ALTER TYPE kbprocessingstatusenum RENAME TO kbprocessingstatusenum_old")
    op.execute(
        "CREATE TYPE kbprocessingstatusenum AS ENUM "
        "('pending', 'uploading', 'processing', 'ready', 'failed', 'pending_upload')"
    )
    op.execute(
        "ALTER TABLE kb_documents ALTER COLUMN processing_status TYPE kbprocessingstatusenum "
        "USING processing_status::text::kbprocessingstatusenum"
    )
    op.execute("ALTER TABLE kb_documents ALTER COLUMN processing_status SET DEFAULT 'pending'")
    op.execute("DROP TYPE kbprocessingstatusenum_old")

    op.execute("ALTER TABLE website_urls ALTER COLUMN crawl_status DROP DEFAULT")
    op.execute("ALTER TYPE wccrawlstatusenum RENAME TO wccrawlstatusenum_old")
    op.execute(
        "CREATE TYPE wccrawlstatusenum AS ENUM "
        "('pending', 'crawling', 'processing', 'ready', 'failed')"
    )
    op.execute(
        "ALTER TABLE website_urls ALTER COLUMN crawl_status TYPE wccrawlstatusenum "
        "USING crawl_status::text::wccrawlstatusenum"
    )
    op.execute("ALTER TABLE website_urls ALTER COLUMN crawl_status SET DEFAULT 'pending'")
    op.execute("DROP TYPE wccrawlstatusenum_old")
