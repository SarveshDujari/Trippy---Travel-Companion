"""Conversation memory and private-workroom delivery."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column("outbox", sa.Column("room_id", sa.String))
    op.add_column(
        "outbox", sa.Column("show_reference", sa.Boolean, nullable=False, server_default=sa.true())
    )
    op.create_table(
        "discussions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("message_id", sa.String, unique=True, nullable=False),
        sa.Column("sender", sa.String, nullable=False),
        sa.Column("request", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("assignment_id", sa.String),
        sa.Column("deadline", sa.Integer),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("context", sa.Text),
    )
    op.create_index("ix_discussions_status", "discussions", ["status"])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("discussion_id", sa.String, sa.ForeignKey("discussions.id"), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_table(
        "travel_facts",
        sa.Column("sender", sa.String, primary_key=True),
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("evidence", sa.Text, nullable=False),
        sa.Column("source_message_id", sa.String, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )


def downgrade():
    raise RuntimeError("Review conversation retention before a destructive downgrade")
