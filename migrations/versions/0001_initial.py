"""Initial persisted collaboration schema, frozen at revision 0001."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "runtime_state",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )
    op.create_table(
        "inbox",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("received_at", sa.Integer, nullable=False),
        sa.Column("outcome", sa.String),
    )
    op.create_table(
        "trips",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("budget_minor", sa.Integer),
        sa.Column("currency", sa.String, nullable=False),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("trip_id", sa.String, sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("start", sa.String, nullable=False),
        sa.Column("duration_minutes", sa.Integer),
        sa.Column("latest_start", sa.String),
        sa.Column("status", sa.String, nullable=False),
    )
    op.create_index("ix_items_trip_id", "items", ["trip_id"])
    op.create_table(
        "dependencies",
        sa.Column("parent", sa.String, sa.ForeignKey("items.id"), primary_key=True),
        sa.Column("child", sa.String, sa.ForeignKey("items.id"), primary_key=True),
        sa.Column("buffer_minutes", sa.Integer, nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("trip_id", sa.String, sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("request", sa.Text),
        sa.Column("assignment_id", sa.String),
        sa.Column("deadline", sa.Integer),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("proposal_id", sa.String),
    )
    op.create_table(
        "proposals",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Integer),
        sa.Column("approved_by", sa.String),
        sa.Column("approved_at", sa.Integer),
    )
    op.create_table(
        "actions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("item_id", sa.String, sa.ForeignKey("items.id"), nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("detail", sa.Text),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dedupe_key", sa.String, unique=True, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt", sa.Integer, nullable=False),
        sa.Column("last_error", sa.String),
    )
    op.create_table(
        "weather_observations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_time", sa.String, nullable=False),
        sa.Column("code", sa.Integer),
        sa.Column("observed_at", sa.Integer, nullable=False),
        sa.Column("notified", sa.Boolean, nullable=False),
        sa.UniqueConstraint("source_time", "code"),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade requires a deliberate data-retention decision")
