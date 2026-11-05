"""Durable inbox/outbox and trip records; short SQLite transactions only."""

from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)

metadata = MetaData()
state = Table(
    "runtime_state",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
)
inbox = Table(
    "inbox",
    metadata,
    Column("id", String, primary_key=True),
    Column("received_at", Integer, nullable=False),
    Column("outcome", String),
)
trips = Table(
    "trips",
    metadata,
    Column("id", String, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("budget_minor", Integer),
    Column("currency", String, nullable=False),
)
items = Table(
    "items",
    metadata,
    Column("id", String, primary_key=True),
    Column("trip_id", ForeignKey("trips.id"), nullable=False, index=True),
    Column("start", String, nullable=False),
    Column("duration_minutes", Integer),
    Column("latest_start", String),
    Column("status", String, nullable=False),
)
edges = Table(
    "dependencies",
    metadata,
    Column("parent", ForeignKey("items.id"), primary_key=True),
    Column("child", ForeignKey("items.id"), primary_key=True),
    Column("buffer_minutes", Integer, nullable=False),
)
tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("trip_id", ForeignKey("trips.id"), nullable=False),
    Column("state", String, nullable=False),
    Column("request", Text),
    Column("assignment_id", String),
    Column("deadline", Integer),
    Column("version", Integer, nullable=False),
    Column("proposal_id", String),
)
proposals = Table(
    "proposals",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", ForeignKey("tasks.id"), nullable=False),
    Column("body", Text, nullable=False),
    Column("expires_at", Integer),
    Column("approved_by", String),
    Column("approved_at", Integer),
    Column("approval_code", String, unique=True, index=True),
)
actions = Table(
    "actions",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", ForeignKey("tasks.id"), nullable=False),
    Column("item_id", ForeignKey("items.id"), nullable=False),
    Column("status", String, nullable=False),
    Column("detail", Text),
)
outbox = Table(
    "outbox",
    metadata,
    Column("attachment_text", Text),
    Column("id", Integer, primary_key=True),
    Column("dedupe_key", String, unique=True, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt", Integer, nullable=False),
    Column("last_error", String),
    Column("room_id", String),
    Column("show_reference", Boolean, nullable=False, default=True),
    Column("related_id", String, index=True),
)
discussions = Table(
    "discussions",
    metadata,
    Column("id", String, primary_key=True),
    Column("message_id", String, unique=True, nullable=False),
    Column("sender", String, nullable=False),
    Column("request", Text, nullable=False),
    Column("status", String, nullable=False, index=True),
    Column("assignment_id", String),
    Column("deadline", Integer),
    Column("created_at", Integer, nullable=False),
    Column("context", Text),
    Column("request_time", Integer),
)
chat = Table(
    "chat_messages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("discussion_id", ForeignKey("discussions.id"), nullable=False),
    Column("role", String, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
)
facts = Table(
    "travel_facts",
    metadata,
    Column("sender", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("source_message_id", String, nullable=False),
    Column("updated_at", Integer, nullable=False),
)
weather = Table(
    "weather_observations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_time", String, nullable=False),
    Column("code", Integer),
    Column("observed_at", Integer, nullable=False),
    Column("notified", Boolean, nullable=False),
    UniqueConstraint("source_time", "code"),
)


preferences = Table(
    "family_preferences",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("subject", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("value", Text, nullable=False),
    Column("hard", Boolean, nullable=False),
    Column("strength", Integer, nullable=False),
    Column("author", String, nullable=False),
    Column("evidence_id", String, nullable=False),
    Column("updated_at", Integer, nullable=False),
    UniqueConstraint("subject", "kind"),
)
expenses = Table(
    "expenses",
    metadata,
    Column("id", String, primary_key=True),
    Column("message_id", String, unique=True),
    Column("payer", String, nullable=False),
    Column("amount_minor", Integer, nullable=False),
    Column("description", Text, nullable=False),
    Column("split", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
)
events = Table(
    "trip_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", Integer, nullable=False),
)
tools = Table(
    "tool_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("discussion_id", ForeignKey("discussions.id"), nullable=False),
    Column("name", String, nullable=False),
    Column("arguments", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("result", Text),
    Column("created_at", Integer, nullable=False),
)


def make_engine(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{Path(path).resolve().as_posix()}")

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

    return engine
