"""Add credentials value to authtypeenum

Revision ID: v010
Revises: v009
Create Date: 2026-05-30
"""
from alembic import op

revision = "v010"
down_revision = "v009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE authtypeenum ADD VALUE IF NOT EXISTS 'credentials'")


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM connector_instances
        USING connector_definitions
        WHERE connector_instances.definition_id = connector_definitions.id
          AND connector_definitions.auth_type = 'credentials'
        """
    )
    op.execute("DELETE FROM connector_definitions WHERE auth_type = 'credentials'")
    op.execute("ALTER TYPE authtypeenum RENAME TO authtypeenum_old")
    op.execute("CREATE TYPE authtypeenum AS ENUM ('oauth2', 'apikey')")
    op.execute(
        "ALTER TABLE connector_definitions ALTER COLUMN auth_type TYPE authtypeenum "
        "USING auth_type::text::authtypeenum"
    )
    op.execute("DROP TYPE authtypeenum_old")
