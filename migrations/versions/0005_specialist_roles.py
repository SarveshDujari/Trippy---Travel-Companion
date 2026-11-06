"""Durable, bounded specialist handoffs within the conversation workflow."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    op.add_column(
        "discussions",
        sa.Column("assigned_role", sa.String, nullable=False, server_default="planner"),
    )
    op.add_column(
        "discussions", sa.Column("specialist_notes", sa.Text, nullable=False, server_default="{}")
    )


def downgrade():
    op.drop_column("discussions", "specialist_notes")
    op.drop_column("discussions", "assigned_role")
