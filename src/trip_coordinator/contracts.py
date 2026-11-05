"""Typed boundary contracts. Agent output is data, never authorization."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(StrictModel):
    addressed: bool = False
    sender: str
    message_id: str
    body: str = Field(max_length=32000)
    timestamp: int
    sender_kind: str | None = None
    attachments: list[dict] = Field(default_factory=list)


class Change(StrictModel):
    item_id: str
    start: datetime
    extra_cost_minor: int = Field(ge=0, le=10000000)
    late_checkin: bool = False
    alternative: Literal["keep", "room_service"] = "keep"

    @field_validator("start")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start requires a timezone")
        return value


class PlanResult(StrictModel):
    kind: Literal["trip_plan"]
    task_id: str
    assignment_id: str
    trip_version: int
    summary: str = Field(min_length=1, max_length=500)
    changes: list[Change] = Field(min_length=1, max_length=10)


class ExecuteResult(StrictModel):
    kind: Literal["trip_execute"]
    task_id: str
    assignment_id: str
    proposal_id: str


class TravelFact(StrictModel):
    key: Literal[
        "destination",
        "dates",
        "travellers",
        "budget",
        "mobility",
        "food",
        "interests",
        "stay_area",
        "transport",
    ]
    value: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=500)


class DiscussionReply(StrictModel):
    kind: Literal["trip_reply"]
    discussion_id: str
    assignment_id: str
    reply: str = Field(min_length=1, max_length=3000)
    facts: list[TravelFact] = Field(default_factory=list, max_length=9)
    uses_weather: bool = False
    claims: list["EvidenceClaim"] = Field(default_factory=list, max_length=12)


class EvidenceClaim(StrictModel):
    source_id: str = Field(min_length=1, max_length=100)
    field: str = Field(min_length=1, max_length=150)
    value: str | int | float | bool | None


class ToolRequest(StrictModel):
    kind: Literal["trip_tool_request"]
    discussion_id: str
    assignment_id: str
    tool: Literal["weather", "places", "route"]
    arguments: dict = Field(default_factory=dict)


DiscussionReply.model_rebuild()


class Member(StrictModel):
    participant_id: str
    can_approve: bool = False


class RuntimeConfig(StrictModel):
    bridge_url: str
    room_id: str
    bridge_id: str
    coordinator_agent_id: str
    planner_participant_id: str
    planner_name: str = "trip-planner"
    reservation_participant_id: str
    reservation_name: str = "trip-reservations"
    members: list[Member] = Field(min_length=1)
    database_path: str = "data/trips.db"
    approval_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    assignment_ttl_seconds: int = Field(default=300, ge=10, le=1800)
    weather_enabled: bool = False
    work_room_id: str | None = None
    conversation_enabled: bool = False

    @field_validator("planner_name", "reservation_name")
    @classmethod
    def safe_agent_name(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
            raise ValueError("agent name must match Switch naming rules")
        return value

    @model_validator(mode="after")
    def distinct_roles(self):
        if self.work_room_id == self.room_id:
            raise ValueError("private workroom must differ from the family room")
        humans = [m.participant_id for m in self.members]
        agents = [self.planner_participant_id, self.reservation_participant_id]
        if len(set(humans)) != len(humans) or len(set(agents)) != 2 or set(humans) & set(agents):
            raise ValueError("participant identities must be unique across roles")
        return self

    @field_validator("bridge_url")
    @classmethod
    def secure_transport(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("bridge URL must not contain credentials, query, or fragment")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("use HTTPS except for a loopback Switch server")
        return value.rstrip("/")
