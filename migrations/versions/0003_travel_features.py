"""Travel features and durable cancellation metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.add_column("outbox", sa.Column("attachment_text", sa.Text))
    op.add_column("outbox", sa.Column("related_id", sa.String))
    op.create_index("ix_outbox_related_id", "outbox", ["related_id"])
    op.add_column("discussions", sa.Column("request_time", sa.Integer))
    op.add_column("proposals", sa.Column("approval_code", sa.String))
    op.create_index("ix_proposals_approval_code", "proposals", ["approval_code"], unique=True)
    op.create_table(
        "family_preferences",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("subject", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("hard", sa.Boolean, nullable=False),
        sa.Column("strength", sa.Integer, nullable=False),
        sa.Column("author", sa.String, nullable=False),
        sa.Column("evidence_id", sa.String, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.UniqueConstraint("subject", "kind"),
    )
    op.create_table(
        "expenses",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("message_id", sa.String, unique=True),
        sa.Column("payer", sa.String, nullable=False),
        sa.Column("amount_minor", sa.Integer, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("split", sa.Text, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_table(
        "trip_events",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_table(
        "tool_jobs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("discussion_id", sa.String, sa.ForeignKey("discussions.id"), nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("arguments", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("result", sa.Text),
        sa.Column("created_at", sa.Integer, nullable=False),
    )


def downgrade():
    raise RuntimeError("Review retention before removing feature records")
