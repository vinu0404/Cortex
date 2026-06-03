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
    op.execute("UPDATE kb_documents SET processing_status = 'pending' WHERE processing_status = 'pending_upload'")
    op.execute("ALTER TABLE kb_documents ALTER COLUMN processing_status DROP DEFAULT")
    op.execute("ALTER TYPE kbprocessingstatusenum RENAME TO kbprocessingstatusenum_old")
    op.execute(
        "CREATE TYPE kbprocessingstatusenum AS ENUM "
        "('pending', 'uploading', 'processing', 'ready', 'failed')"
    )
    op.execute(
        "ALTER TABLE kb_documents ALTER COLUMN processing_status TYPE kbprocessingstatusenum "
        "USING processing_status::text::kbprocessingstatusenum"
    )
    op.execute("ALTER TABLE kb_documents ALTER COLUMN processing_status SET DEFAULT 'pending'")
    op.execute("DROP TYPE kbprocessingstatusenum_old")
