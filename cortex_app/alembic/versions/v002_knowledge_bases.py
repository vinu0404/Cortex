"""Knowledge bases tables

Revision ID: v002
Revises: v001
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "v002"
down_revision = "v001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    kbsourcetypeenum = postgresql.ENUM("device", "s3_url", "gdrive", name="kbsourcetypeenum", create_type=False)
    kbsourcetypeenum.create(op.get_bind(), checkfirst=True)

    kbprocessingstatusenum = postgresql.ENUM(
        "pending", "uploading", "processing", "ready", "failed",
        name="kbprocessingstatusenum", create_type=False,
    )
    kbprocessingstatusenum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "knowledge_bases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_bases_user_id", "knowledge_bases", ["user_id"])

    op.create_table(
        "kb_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("file_hash", sa.String(64), nullable=True),
        sa.Column("storage_key", sa.String(), nullable=True),
        sa.Column("staging_path", sa.String(), nullable=True),
        sa.Column("source_type", sa.Enum("device", "s3_url", "gdrive", name="kbsourcetypeenum"), nullable=False, server_default="device"),
        sa.Column("processing_status", sa.Enum("pending", "uploading", "processing", "ready", "failed", name="kbprocessingstatusenum"), nullable=False, server_default="pending"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.String(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kb_documents_kb_id", "kb_documents", ["kb_id"])
    op.create_index("ix_kb_documents_user_id", "kb_documents", ["user_id"])
    op.create_index("ix_kb_documents_file_hash", "kb_documents", ["file_hash"])
    op.create_index("ix_kb_documents_status", "kb_documents", ["processing_status"])

    op.create_table(
        "agent_knowledge_bases",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "kb_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_knowledge_bases")
    op.drop_table("kb_documents")
    op.drop_table("knowledge_bases")
    op.execute("DROP TYPE IF EXISTS kbprocessingstatusenum")
    op.execute("DROP TYPE IF EXISTS kbsourcetypeenum")
