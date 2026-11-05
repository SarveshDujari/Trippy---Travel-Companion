"""Application-owned orchestration behind the three Switch participants.

Coordinator routing is deterministic. Planner/reservation decisions arrive from
distinct authenticated Switch participants; tests inject protocol fixtures only.
"""

import hashlib
import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import insert, select, text, update

from . import discussion
from . import storage as db
from .contracts import (
    DiscussionReply,
    ExecuteResult,
    Message,
    PlanResult,
    RuntimeConfig,
    ToolRequest,
)
from .grounding import verify

TRIP = "hyderabad-demo"
QUOTES = {"transfer": 30000, "hotel": 0, "dinner": 20000}
FINAL = {"completed", "partially_completed", "failed", "cancelled"}


class Rejected(ValueError):
    """A request failed an application rule."""


def identifier(prefix):
    return f"{prefix}_{uuid4().hex[:12]}"


def row(connection, table, identity):
    value = connection.execute(select(table).where(table.c.id == identity)).mappings().first()
    if value is None:
        raise Rejected(f"Unknown {table.name} reference")
    return dict(value)


def enqueue(
    connection,
    key,
    body,
    now,
    *,
    room_id=None,
    show_reference=True,
    related_id=None,
    attachment_text=None,
    media_json=None,
):
    connection.execute(
        insert(db.outbox).values(
            dedupe_key=key,
            body=body,
            status="pending",
            attempts=0,
            next_attempt=now,
            room_id=room_id,
            show_reference=show_reference,
            related_id=related_id,
            attachment_text=attachment_text,
            media_json=media_json,
        )
    )


def seed(engine):
    """An explicit fictional fixture, never the user's actual trip."""
    with engine.begin() as c:
        if c.execute(select(db.trips.c.id)).first():
            return
        c.execute(insert(db.trips).values(id=TRIP, version=1, budget_minor=250000, currency="INR"))
        c.execute(
            insert(db.items),
            [
                {
                    "id": name,
                    "trip_id": TRIP,
                    "start": f"2026-11-06T{start}:00+05:30",
                    "duration_minutes": duration,
                    "latest_start": latest,
                    "status": "simulated_confirmed",
                }
                for name, start, duration, latest in [
                    ("flight", "19:30", 0, None),
                    ("transfer", "18:00", 45, None),
                    ("hotel", "19:00", 30, "2026-11-06T22:00:00+05:30"),
                    ("dinner", "20:30", 60, "2026-11-06T23:00:00+05:30"),
                ]
            ],
        )
        c.execute(
            insert(db.edges),
            [
                {"parent": "flight", "child": "transfer", "buffer_minutes": 45},
                {"parent": "transfer", "child": "hotel", "buffer_minutes": 0},
                {"parent": "hotel", "child": "dinner", "buffer_minutes": 15},
            ],
        )


def planning_context(c):
    affected = (
        c.execute(
            text(
                "WITH RECURSIVE affected(id) AS (SELECT child FROM dependencies WHERE parent=:root "
                "UNION SELECT d.child FROM dependencies d JOIN affected a ON d.parent=a.id) "
                "SELECT id FROM affected ORDER BY id"
            ),
            {"root": "flight"},
        )
        .scalars()
        .all()
    )
    return {
        "mode": "SIMULATED HYDERABAD FIXTURE; all prices are simulator quotes",
        "trip": row(c, db.trips, TRIP),
        "items": [dict(x) for x in c.execute(select(db.items)).mappings()],
        "dependencies": [dict(x) for x in c.execute(select(db.edges)).mappings()],
        "affected_ids": affected,
        "change_quotes_minor": QUOTES,
        "simulator_options": {
            "late_hotel_checkin": "up to four hours after original deadline",
            "dinner_room_service": "up to four hours after dinner deadline",
        },
        "instruction": (
            "Repair delayed arrival. Change only transfer/hotel/dinner; preserve flight."
        ),
    }


def validate_plan(c, result):
    trip = row(c, db.trips, TRIP)
    if trip["version"] != result.trip_version:
        raise Rejected("Trip changed; a new plan and approval are required")
    current = {x["id"]: dict(x) for x in c.execute(select(db.items)).mappings()}
    ids = [x.item_id for x in result.changes]
    if len(set(ids)) != len(ids) or any(x not in QUOTES for x in ids):
        raise Rejected("Plan contains duplicate or unsupported reservation changes")
    if any(x.extra_cost_minor != QUOTES[x.item_id] for x in result.changes):
        raise Rejected("Plan prices do not match current simulator quotes")
    if sum(x.extra_cost_minor for x in result.changes) > trip["budget_minor"]:
        raise Rejected("Plan exceeds the extra-spending limit")
    for change in result.changes:
        original = datetime.fromisoformat(current[change.item_id]["start"])
        if abs((change.start - original).total_seconds()) > 86400:
            raise Rejected("Change is outside the supported one-day repair window")
        current[change.item_id]["start"] = change.start.isoformat()
    for item in current.values():
        late = any(x.item_id == item["id"] and x.late_checkin for x in result.changes)
        if late and item["id"] != "hotel":
            raise Rejected("Late check-in exception applies only to the simulated hotel")
        room_service = any(
            x.item_id == item["id"] and x.alternative == "room_service" for x in result.changes
        )
        if any(x.alternative == "room_service" and x.item_id != "dinner" for x in result.changes):
            raise Rejected("Room service is only a simulated dinner alternative")
        extension = timedelta(hours=4 if late or room_service else 0)
        if item["latest_start"] and (
            datetime.fromisoformat(item["start"])
            > datetime.fromisoformat(item["latest_start"]) + extension
        ):
            raise Rejected(f"{item['id']} would start after its deadline")
    for edge in c.execute(select(db.edges)).mappings():
        parent, child = current[edge["parent"]], current[edge["child"]]
        earliest = datetime.fromisoformat(parent["start"]) + timedelta(
            minutes=parent["duration_minutes"] + edge["buffer_minutes"]
        )
        if datetime.fromisoformat(child["start"]) < earliest:
            raise Rejected(f"Timing conflict: {child['id']} follows {parent['id']} too soon")


class Coordinator:
    def __init__(self, engine, config: RuntimeConfig, clock=time.time):
        self.engine, self.config, self.clock = engine, config, clock
        self.members = {m.participant_id: m for m in config.members}
        if engine is not None:
            from .family import sync_members

            with engine.begin() as c:
                sync_members(c, config, int(clock()))

    def consume(self, envelope):
        """Only call with frames received over the authenticated Switch connection."""
        now = int(self.clock())
        incoming_room = envelope.get("room_id")
        if incoming_room is None or incoming_room not in {
            self.config.room_id,
            self.config.work_room_id,
        }:
            return
        expected_bridge = self.config.bridge_id if incoming_room == self.config.room_id else None
        if envelope.get("bridge_id") != expected_bridge:
            return
        with self.engine.begin() as c:
            if envelope.get("type") == "message":
                payload = envelope.get("payload", {})
                # Switch can add optional event fields across revisions; normalize at boundary.
                allowed = Message.model_fields
                try:
                    message = Message.model_validate(
                        {k: v for k, v in payload.items() if k in allowed}
                    )
                except (ValidationError, TypeError, AttributeError):
                    digest = hashlib.sha256(
                        json.dumps(payload, sort_keys=True).encode()
                    ).hexdigest()
                    key = "quarantine:" + digest
                    if not c.execute(select(db.inbox.c.id).where(db.inbox.c.id == key)).first():
                        c.execute(
                            insert(db.inbox).values(id=key, received_at=now, outcome="quarantined")
                        )
                    if isinstance(envelope.get("sequence"), int):
                        self._state(c, "cursor", str(envelope["sequence"]))
                    return
                key = f"{self.config.bridge_id}:{self.config.room_id}:{message.message_id}"
                if not c.execute(select(db.inbox.c.id).where(db.inbox.c.id == key)).first():
                    c.execute(insert(db.inbox).values(id=key, received_at=now, outcome="ignored"))
                    try:
                        with c.begin_nested():
                            outcome = self._route(c, message, now, incoming_room)
                    except (Rejected, ValidationError, json.JSONDecodeError) as exc:
                        # Do not echo model output or provider errors into chat/logs.
                        reason = (
                            str(exc) if isinstance(exc, Rejected) else "Invalid agent result format"
                        )
                        if (
                            isinstance(exc, ValidationError)
                            and message.sender == self.config.planner_participant_id
                            and discussion.retry_schema_reply(c, message, now)
                        ):
                            outcome = "retrying_agent_reply_schema"
                        else:
                            enqueue(c, key + ":rejected", f"Request not applied: {reason}.", now)
                            outcome = "rejected"
                    c.execute(update(db.inbox).where(db.inbox.c.id == key).values(outcome=outcome))
            sequence = envelope.get("sequence")
            if isinstance(sequence, int):
                self._state(c, "cursor", str(sequence))

    @staticmethod
    def _state(c, key, value):
        if c.execute(select(db.state.c.key).where(db.state.c.key == key)).first():
            c.execute(update(db.state).where(db.state.c.key == key).values(value=value))
        else:
            c.execute(insert(db.state).values(key=key, value=value))

    def _route(self, c, message, now, incoming_room=None):
        if message.sender_kind == "platform":
            return "ignored_platform"
        if message.sender == self.config.planner_participant_id:
            if self.config.work_room_id and incoming_room != self.config.work_room_id:
                return "ignored_wrong_agent_room"
            if message.body.lstrip().startswith("{"):
                if json.loads(message.body).get("kind") == "trip_tool_request":
                    request = ToolRequest.model_validate_json(message.body)
                    try:
                        discussion.request_tool(c, request, now)
                    except ValueError as exc:
                        raise Rejected(str(exc)) from exc
                    return "tool_requested"
                if json.loads(message.body).get("kind") == "trip_reply":
                    reply = DiscussionReply.model_validate_json(message.body)
                    try:
                        accepted = discussion.accept(c, reply, now)
                    except ValueError as exc:
                        raise Rejected(str(exc)) from exc
                    if not accepted:
                        return "ignored_stale_discussion"
                    text_reply = reply.reply
                    saved = row(c, db.discussions, reply.discussion_id)
                    from .family import propose_import, propose_member, propose_preference

                    try:
                        additions = [
                            propose_preference(c, draft, saved, now) for draft in reply.preferences
                        ]
                        if reply.member_action:
                            additions.append(propose_member(c, reply.member_action, saved, now))
                        if reply.image:
                            text_reply = (
                                "From your image — unconfirmed, please check:\n" + text_reply
                            )
                            imported = propose_import(c, reply.image, saved, now)
                            if imported:
                                additions.append(imported)
                        if additions:
                            text_reply += "\n\n" + "\n\n".join(additions)
                    except ValueError as exc:
                        raise Rejected(str(exc)) from exc
                    citations = verify(reply, json.loads(saved["context"]), saved["request"])
                    if citations:
                        text_reply += "\n" + citations
                    from .comparison import card

                    for evidence in json.loads(saved["context"]).get("evidence", {}).values():
                        if evidence.get(
                            "status"
                        ) == "available" and "comparison_id" in evidence.get("data", {}):
                            text_reply += "\n\n" + card(evidence["data"])
                    if reply.uses_weather:
                        saved = row(c, db.discussions, reply.discussion_id)
                        weather_data = json.loads(saved["context"])["weather"]
                        text_reply += (
                            "\n\nWeather: Open-Meteo (https://open-meteo.com/)"
                            + " · checked "
                            + weather_data["checked_at"]
                        )
                    enqueue(
                        c,
                        reply.discussion_id + ":reply",
                        text_reply,
                        now,
                        show_reference=False,
                        related_id=reply.discussion_id,
                    )
                    if reply.visual_plan:
                        from .media import render_plan, visual_requested

                        attempted = c.execute(
                            select(db.tools.c.id).where(
                                db.tools.c.discussion_id == reply.discussion_id,
                                db.tools.c.name == "compare_plans",
                            )
                        ).first()
                        comparison_id = next(
                            (
                                e["data"]["comparison_id"]
                                for e in json.loads(saved["context"]).get("evidence", {}).values()
                                if e.get("status") == "available"
                                and "comparison_id" in e.get("data", {})
                            ),
                            None,
                        )
                        if visual_requested(saved["request"]) and (not attempted or comparison_id):
                            enqueue(
                                c,
                                reply.discussion_id + ":visual",
                                "Your plan at a glance. Estimates and unknowns are labelled.",
                                now,
                                show_reference=False,
                                media_json=json.dumps(render_plan(c, comparison_id=comparison_id)),
                                related_id=reply.discussion_id,
                            )
                    return "discussion_reply"
                self._plan(c, PlanResult.model_validate_json(message.body), now)
                return "plan_received"
            return "ignored_agent_commentary"
        if message.sender == self.config.reservation_participant_id:
            if self.config.work_room_id and incoming_room != self.config.work_room_id:
                return "ignored_wrong_agent_room"
            if message.body.lstrip().startswith("{"):
                self._execute(c, ExecuteResult.model_validate_json(message.body), now)
                return "execution_requested"
            return "ignored_agent_commentary"
        member = self.members.get(message.sender)
        if member is None or (incoming_room is not None and incoming_room != self.config.room_id):
            return "ignored"
        # Old/replayed human commands cannot approve or mutate current state.
        if abs(now - message.timestamp / 1000) > 120:
            raise Rejected("Command is older than two minutes; send a fresh command")
        from .family import command as family_command

        try:
            answer = family_command(c, message.body, message.sender, message.message_id, now)
        except ValueError as exc:
            raise Rejected(str(exc)) from exc
        if answer:
            enqueue(c, message.message_id + ":family", answer, now, show_reference=False)
            return "family_command"
        if message.body.strip().lower() in {
            "trip approvals",
            "show pending approvals",
            "show the proposal",
        }:
            from .approvals import pending

            enqueue(
                c, message.message_id + ":approvals", pending(c, now), now, show_reference=False
            )
            return "approval_status"
        short = re.fullmatch(
            r"(approve|reject) ([A-Fa-f0-9]{6}|proposal_[0-9a-f]{12})", message.body.strip()
        )
        if short:
            if not member.can_approve:
                raise Rejected("Your linked family membership cannot approve spending")
            if short[1] == "approve":
                self._approve(c, short[2], message.sender, now)
            else:
                self._reject(c, short[2], now)
            return "human_decision"
        if not message.body.startswith("trip "):
            return self._discuss(c, message, now)
        command = message.body[5:].strip()
        if command in {"visual", "timeline", "map"}:
            from .media import render_plan

            enqueue(
                c,
                message.message_id + ":visual",
                "Your plan at a glance — schematic, not a geographic map.",
                now,
                show_reference=False,
                media_json=json.dumps(render_plan(c)),
            )
            return "visual_plan"
        if command == "calendar":
            from .features import calendar

            enqueue(
                c,
                message.message_id + ":calendar",
                "SIMULATED itinerary calendar; no real bookings.",
                now,
                show_reference=False,
                attachment_text=calendar(c).decode("utf-8"),
            )
            return "calendar_export"
        from .features import chat_command

        try:
            feature_reply = chat_command(c, command, message, now)
        except ValueError as exc:
            raise Rejected(str(exc)) from exc
        if feature_reply is not None:
            enqueue(c, message.message_id + ":feature", feature_reply, now, show_reference=False)
            return "travel_feature"
        if command == "forget":
            discussion.forget(c, message.sender)
            enqueue(
                c,
                message.message_id + ":forget",
                "I've cleared your discussion history and saved travel preferences from "
                "my local conversation memory. Booking audit records remain separate.",
                now,
                show_reference=False,
            )
            return "discussion_forgotten"
        if command == "status":
            value = planning_context(c)
            lines = [f"SIMULATED Hyderabad trip, version {value['trip']['version']}"]
            lines += [f"{x['id']}: {x['start']} ({x['status']})" for x in value["items"]]
            enqueue(c, message.message_id + ":status", "\n".join(lines), now)
        elif command.startswith("plan "):
            self._start(c, command[5:1005], now)
        elif command.startswith("approve "):
            if not member.can_approve:
                raise Rejected("Your linked family membership cannot approve spending")
            self._approve(c, command[8:].strip(), message.sender, now)
        elif command.startswith("reject "):
            if not member.can_approve:
                raise Rejected("Your linked family membership cannot reject proposals")
            self._reject(c, command[7:].strip(), now)
        else:
            return self._discuss(c, message, now)
        return "human_command"

    def _discuss(self, c, message, now):
        if not self.config.conversation_enabled:
            return "ignored"
        if message.attachments:
            from .media import validate_references

            try:
                validate_references(message.attachments)
            except ValueError as exc:
                raise Rejected(str(exc)) from exc
        result = discussion.queue(c, message, now)
        if result == "busy":
            enqueue(
                c,
                message.message_id + ":busy",
                "I'm catching up on the trip discussion—please give me a moment.",
                now,
                show_reference=False,
            )
        return "discussion_queued"

    def _start(self, c, request, now):
        if c.execute(select(db.tasks.c.id).where(~db.tasks.c.state.in_(FINAL))).first():
            raise Rejected("A trip modification is already active")
        context = planning_context(c)
        task_id, assignment = identifier("task"), identifier("assignment")
        c.execute(
            insert(db.tasks).values(
                id=task_id,
                trip_id=TRIP,
                state="planning",
                request=request,
                assignment_id=assignment,
                deadline=now + self.config.assignment_ttl_seconds,
                version=context["trip"]["version"],
            )
        )
        payload = {
            "task_id": task_id,
            "assignment_id": assignment,
            "request": request,
            "context": context,
        }
        enqueue(
            c,
            assignment,
            f"@{self.config.planner_name} PLAN_ASSIGNMENT\n" + json.dumps(payload),
            now,
            room_id=self.config.work_room_id,
        )

    def _assignment(self, c, result, expected_state, now):
        task = row(c, db.tasks, result.task_id)
        if task["state"] != expected_state or task["assignment_id"] != result.assignment_id:
            raise Rejected("Result belongs to an inactive assignment")
        if task["deadline"] < now:
            raise Rejected("Assignment expired")
        return task

    def _plan(self, c, result, now):
        task = self._assignment(c, result, "planning", now)
        if result.trip_version != task["version"]:
            raise Rejected("Result does not match the assigned trip version")
        validate_plan(c, result)
        proposal = identifier("proposal")
        code = secrets.token_hex(3).upper()
        while c.execute(
            select(db.proposals.c.id).where(db.proposals.c.approval_code == code)
        ).first():
            code = secrets.token_hex(3).upper()
        c.execute(
            insert(db.proposals).values(
                id=proposal,
                task_id=result.task_id,
                body=result.model_dump_json(),
                expires_at=now + self.config.approval_ttl_seconds,
                approval_code=code,
            )
        )
        c.execute(
            update(db.tasks)
            .where(db.tasks.c.id == result.task_id)
            .values(state="awaiting_approval", proposal_id=proposal, deadline=None)
        )
        before = {x.item_id: row(c, db.items, x.item_id)["start"] for x in result.changes}
        c.execute(
            insert(db.events).values(
                id=proposal + ":before",
                kind="proposal_snapshot",
                payload=json.dumps(before),
                status="recorded",
                created_at=now,
            )
        )
        from .approvals import card

        enqueue(
            c, proposal, card(c, row(c, db.proposals, proposal), now), now, show_reference=False
        )

    def _approve(self, c, proposal_id, sender, now):
        proposal_id = self._resolve_proposal(c, proposal_id)
        proposal = row(c, db.proposals, proposal_id)
        task = row(c, db.tasks, proposal["task_id"])
        if task["state"] != "awaiting_approval" or task["proposal_id"] != proposal_id:
            raise Rejected("Proposal is no longer awaiting approval")
        if proposal["expires_at"] < now:
            raise Rejected("Proposal expired; request a new plan after task expiry")
        result = PlanResult.model_validate_json(proposal["body"])
        validate_plan(c, result)
        assignment = identifier("assignment")
        c.execute(
            update(db.proposals)
            .where(db.proposals.c.id == proposal_id)
            .values(approved_by=sender, approved_at=now)
        )
        c.execute(
            update(db.tasks)
            .where(db.tasks.c.id == task["id"])
            .values(
                state="executing",
                assignment_id=assignment,
                deadline=now + self.config.assignment_ttl_seconds,
            )
        )
        payload = {
            "task_id": task["id"],
            "assignment_id": assignment,
            "proposal_id": proposal_id,
            "mode": "SIMULATED",
            "changes": result.model_dump(mode="json")["changes"],
        }
        enqueue(
            c,
            assignment,
            f"@{self.config.reservation_name} EXECUTION_ASSIGNMENT\n" + json.dumps(payload),
            now,
            room_id=self.config.work_room_id,
        )

    @staticmethod
    def _resolve_proposal(c, reference):
        if reference.startswith("proposal_"):
            return reference
        value = c.execute(
            select(db.proposals.c.id).where(db.proposals.c.approval_code == reference.upper())
        ).scalar()
        if value is None:
            raise Rejected("Unknown approval code")
        return value

    def _reject(self, c, reference, now):
        proposal = row(c, db.proposals, self._resolve_proposal(c, reference))
        task = row(c, db.tasks, proposal["task_id"])
        if task["state"] != "awaiting_approval" or proposal["expires_at"] < now:
            raise Rejected("Proposal is no longer awaiting a current decision")
        c.execute(update(db.tasks).where(db.tasks.c.id == task["id"]).values(state="cancelled"))
        enqueue(
            c,
            proposal["id"] + ":rejected",
            "Proposal declined. No changes were made.",
            now,
            show_reference=False,
        )

    def _execute(self, c, result, now):
        task = self._assignment(c, result, "executing", now)
        if task["proposal_id"] != result.proposal_id:
            raise Rejected("Execution references a different proposal")
        proposal = row(c, db.proposals, result.proposal_id)
        if not proposal["approved_by"] or proposal["expires_at"] < now:
            raise Rejected("Execution has no current scoped approval")
        plan = PlanResult.model_validate_json(proposal["body"])
        validate_plan(c, plan)
        lines, failed = ["SIMULATED execution results:"], False
        for change in plan.changes:
            # This local simulator is transactional and has no network side effects.
            # Real providers require intent/attempt persistence and reconciliation.
            status = (
                "failed"
                if change.item_id == "dinner" and change.alternative == "keep"
                else "simulated_confirmed"
            )
            failed |= status == "failed"
            detail = "Requested slot unavailable" if status == "failed" else "Simulator confirmed"
            c.execute(
                insert(db.actions).values(
                    id=f"{result.proposal_id}:{change.item_id}",
                    task_id=task["id"],
                    item_id=change.item_id,
                    status=status,
                    detail=detail,
                )
            )
            if status != "failed":
                c.execute(
                    update(db.items)
                    .where(db.items.c.id == change.item_id)
                    .values(start=change.start.isoformat(), status=status)
                )
            lines.append(f"{change.item_id}: {detail}")
        c.execute(
            update(db.trips).where(db.trips.c.id == TRIP).values(version=plan.trip_version + 1)
        )
        c.execute(
            update(db.tasks)
            .where(db.tasks.c.id == task["id"])
            .values(state="partially_completed" if failed else "completed", deadline=None)
        )
        if failed:
            lines.append("Confirmed changes are retained. Dinner needs a new plan and approval.")
        enqueue(c, result.proposal_id + ":outcome", "\n".join(lines), now)

    def expire(self):
        now = int(self.clock())
        with self.engine.begin() as c:
            for task in c.execute(select(db.tasks).where(~db.tasks.c.state.in_(FINAL))).mappings():
                deadline = task["deadline"]
                if task["state"] == "awaiting_approval":
                    deadline = row(c, db.proposals, task["proposal_id"])["expires_at"]
                if deadline is not None and deadline < now:
                    c.execute(
                        update(db.tasks).where(db.tasks.c.id == task["id"]).values(state="failed")
                    )
                    enqueue(
                        c,
                        task["id"] + ":expired",
                        "Task expired without further changes. Send trip plan <request> to retry.",
                        now,
                    )


def utc_now():
    return datetime.now(UTC)
