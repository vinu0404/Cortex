"""Website collections tables

Revision ID: v003
Revises: v002
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "v003"
down_revision = "v002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgresql.ENUM("pending", "crawling", "processing", "ready", "failed", name="wccrawlstatusenum", create_type=False).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "website_collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("url_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_website_collections_user_id", "website_collections", ["user_id"])

    op.create_table(
        "website_urls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("website_collections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("max_depth", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("crawl_status", postgresql.ENUM("pending", "crawling", "processing", "ready", "failed", name="wccrawlstatusenum", create_type=False), nullable=False, server_default="pending"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("login_blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_website_urls_collection_id", "website_urls", ["collection_id"])
    op.create_index("ix_website_urls_user_id", "website_urls", ["user_id"])
    op.create_index("ix_website_urls_crawl_status", "website_urls", ["crawl_status"])

    op.create_table(
        "agent_website_collections",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("website_collections.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "collection_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_website_collections")
    op.drop_table("website_urls")
    op.drop_table("website_collections")
    op.execute("DROP TYPE IF EXISTS wccrawlstatusenum")
