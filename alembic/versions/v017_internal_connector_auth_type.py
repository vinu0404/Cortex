"""Add internal value to authtypeenum for built-in platform connectors

Revision ID: v017
Revises: v016
Create Date: 2026-05-31
"""
from alembic import op

revision = "v017"
down_revision = "v016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE authtypeenum ADD VALUE IF NOT EXISTS 'internal'")


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM connector_instances
        USING connector_definitions
        WHERE connector_instances.definition_id = connector_definitions.id
          AND connector_definitions.auth_type = 'internal'
        """
    )
    op.execute("DELETE FROM connector_definitions WHERE auth_type = 'internal'")
    op.execute("ALTER TYPE authtypeenum RENAME TO authtypeenum_old")
    op.execute("CREATE TYPE authtypeenum AS ENUM ('oauth2', 'apikey', 'credentials')")
    op.execute(
        "ALTER TABLE connector_definitions ALTER COLUMN auth_type TYPE authtypeenum "
        "USING auth_type::text::authtypeenum"
    )
    op.execute("DROP TYPE authtypeenum_old")
