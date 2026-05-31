"""unique (user_id, name) constraints on workspaces, knowledge_bases, website_collections

Revision ID: v016
Revises: v015
Create Date: 2026-05-31
"""
from alembic import op

revision = "v016"
down_revision = "v015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deduplicate workspaces before adding unique index.
    # Keep the newest row per (user_id, name) where deleted_at IS NULL;
    # soft-delete older duplicates by stamping deleted_at = now().
    op.execute("""
        UPDATE workspaces
        SET deleted_at = NOW()
        WHERE deleted_at IS NULL
          AND id NOT IN (
              SELECT DISTINCT ON (user_id, name) id
              FROM workspaces
              WHERE deleted_at IS NULL
              ORDER BY user_id, name, created_at DESC
          )
    """)

    # Deduplicate knowledge_bases — rename older duplicates to avoid hard-deleting data.
    op.execute("""
        UPDATE knowledge_bases
        SET name = name || ' (' || LEFT(id::text, 8) || ')'
        WHERE id NOT IN (
            SELECT DISTINCT ON (user_id, name) id
            FROM knowledge_bases
            ORDER BY user_id, name, created_at DESC
        )
    """)

    # Deduplicate website_collections — same rename approach.
    op.execute("""
        UPDATE website_collections
        SET name = name || ' (' || LEFT(id::text, 8) || ')'
        WHERE id NOT IN (
            SELECT DISTINCT ON (user_id, name) id
            FROM website_collections
            ORDER BY user_id, name, created_at DESC
        )
    """)

    # Now safe to create constraints.
    op.execute(
        "CREATE UNIQUE INDEX uq_workspaces_user_name_active "
        "ON workspaces (user_id, name) WHERE deleted_at IS NULL"
    )

    op.create_unique_constraint(
        "uq_knowledge_bases_user_name", "knowledge_bases", ["user_id", "name"]
    )
    op.create_unique_constraint(
        "uq_website_collections_user_name", "website_collections", ["user_id", "name"]
    )


def downgrade() -> None:
    op.drop_index("uq_workspaces_user_name_active", table_name="workspaces")
    op.drop_constraint("uq_knowledge_bases_user_name", "knowledge_bases", type_="unique")
    op.drop_constraint("uq_website_collections_user_name", "website_collections", type_="unique")
