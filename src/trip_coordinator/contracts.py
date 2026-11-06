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
    preferences: list["PreferenceDraft"] = Field(default_factory=list, max_length=4)
    member_action: "MemberDraft | None" = None
    image: "ImageReading | None" = None
    visual_plan: bool = False
    assistant_action: (
        Literal["family", "approvals", "calendar", "status", "propose_changes"] | None
    ) = None


class EvidenceClaim(StrictModel):
    source_id: str = Field(min_length=1, max_length=100)
    field: str = Field(min_length=1, max_length=150)
    value: str | int | float | bool | None


class ToolRequest(StrictModel):
    kind: Literal["trip_tool_request"]
    discussion_id: str
    assignment_id: str
    tool: Literal["weather", "places", "route", "compare_plans"]
    arguments: dict = Field(default_factory=dict)


class PreferenceDraft(StrictModel):
    subject: str = Field(min_length=1, max_length=100)
    kind: Literal[
        "max_walk_m",
        "fatigue_limit",
        "vegetarian",
        "halal",
        "required_place",
        "wheelchair",
        "senior",
    ]
    value: str | int | bool
    hard: bool = True
    strength: int = Field(default=3, ge=1, le=5)
    evidence: str = Field(min_length=1, max_length=500)
    operation: Literal["set", "remove"] = "set"


class MemberDraft(StrictModel):
    action: Literal["rename_self", "link_alias"]
    name: str = Field(min_length=1, max_length=80)
    member_id: str | None = None
    evidence: str = Field(min_length=1, max_length=500)


class ImportItem(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["flight", "train", "hotel", "activity", "transfer", "other"]
    start: datetime | None = None
    end: datetime | None = None
    location: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def valid_times(self):
        if any(x is not None and x.tzinfo is None for x in [self.start, self.end]):
            raise ValueError("Image itinerary times require an explicit timezone")
        if self.start and self.end and self.end <= self.start:
            raise ValueError("Arrival/end must be after departure/start")
        return self


class ImageReading(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    extracted_text: str = Field(default="", max_length=4000)
    uncertainties: list[str] = Field(default_factory=list, max_length=12)
    itinerary: list[ImportItem] = Field(default_factory=list, max_length=8)


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
