"""Bounded expert handoffs. Specialists advise; only the Coordinator can act."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update

from . import storage as db
from .contracts import DiscussionReply, Message
from .grounding import verify

NAMES = {"researcher": "trip-researcher", "mediator": "trip-mediator", "watcher": "trip-watcher"}


def participant(config, role):
    return (
        config.planner_participant_id
        if role == "planner"
        else getattr(config, role + "_participant_id")
    )


def active(c, identity, assignment, now):
    row = (
        c.execute(select(db.discussions).where(db.discussions.c.id == identity)).mappings().first()
    )
    if (
        not row
        or row["status"] != "waiting"
        or row["assignment_id"] != assignment
        or row["deadline"] < now
    ):
        raise ValueError("Specialist handoff belongs to inactive work")
    return row


def delegate(c, request, config, now):
    current = active(c, request.discussion_id, request.assignment_id, now)
    if current["assigned_role"] != "planner":
        raise ValueError("Only the Planner can delegate")
    notes = json.loads(current["specialist_notes"])
    if request.role in notes:
        raise ValueError("Each specialist may be called only once per discussion")
    if not participant(config, request.role):
        raise ValueError("This specialist is not configured; continue with existing tools")
    if request.role == "mediator":
        evidence = json.loads(current["context"]).get("evidence", {})
        if not any(x.get("data", {}).get("plans") for x in evidence.values()):
            raise ValueError("Mediator requires computed plan options first")
    notes[request.role] = {"status": "requested", "objective": request.objective}
    c.execute(
        update(db.discussions)
        .where(db.discussions.c.id == current["id"])
        .values(assigned_role=request.role, specialist_notes=json.dumps(notes), status="queued")
    )


def accept(c, result, config, now):
    from .service import enqueue

    current = active(c, result.discussion_id, result.assignment_id, now)
    if current["assigned_role"] != result.role:
        raise ValueError("Specialist result has the wrong role")
    snapshot = json.loads(current["context"])
    proxy = DiscussionReply(
        kind="trip_reply",
        discussion_id=result.discussion_id,
        assignment_id=result.assignment_id,
        reply=result.summary,
        claims=result.claims,
    )
    citations = verify(proxy, snapshot, current["request"])
    if result.role == "mediator":
        comparisons = [
            x["data"]
            for x in snapshot.get("evidence", {}).values()
            if x.get("status") == "available" and x.get("data", {}).get("plans")
        ]
        if not comparisons:
            raise ValueError("Mediator needs computed options")
        if result.recommended_option is None:
            if any(p["evaluation"]["feasible"] for p in comparisons[-1]["plans"]):
                raise ValueError("Mediator should select an available feasible option")
        elif not comparisons[-1]["plans"][result.recommended_option]["evaluation"]["feasible"]:
            raise ValueError("Mediator cannot recommend a plan blocked by hard constraints")
    elif result.recommended_option is not None:
        raise ValueError("Only the Mediator selects plan options")
    if result.role != "watcher" and (result.affected_items or result.replan_suggested):
        raise ValueError("Only the Watcher assesses disruptions")
    notes = json.loads(current["specialist_notes"])
    notes[result.role] = {
        "status": "completed",
        "advice": result.summary,
        "verified_facts": citations,
        "recommended_option": result.recommended_option,
    }
    if result.role == "watcher":
        allowed = set(notes.get("eligible_items", []))
        if set(result.affected_items) - allowed:
            raise ValueError("Watcher cited an unknown itinerary item")
        if not result.claims:
            raise ValueError("Watcher alert needs source evidence")
        # Do not relay model prose, which could overstate a disruption.
        if quiet(config, now):
            c.execute(
                update(db.discussions)
                .where(db.discussions.c.id == current["id"])
                .values(status="completed", specialist_notes=json.dumps(notes))
            )
            return
        enqueue(
            c,
            current["id"] + ":watcher-alert",
            "Trip Watcher advisory — current conditions, not a booking change.\n"
            + citations
            + "\nItems to review: "
            + (", ".join(result.affected_items) or "none identified")
            + ("\nAsk me to prepare alternatives." if result.replan_suggested else ""),
            now,
            show_reference=False,
            related_id=current["id"],
        )
        c.execute(
            update(db.discussions)
            .where(db.discussions.c.id == current["id"])
            .values(status="completed", specialist_notes=json.dumps(notes))
        )
    else:
        c.execute(
            update(db.discussions)
            .where(db.discussions.c.id == current["id"])
            .values(status="queued", assigned_role="planner", specialist_notes=json.dumps(notes))
        )


def quiet(config, now):
    hour = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).hour
    start, end = config.watcher_quiet_start, config.watcher_quiet_end
    return start != end and (start <= hour < end if start < end else hour >= start or hour < end)


def weather_event(c, config, source_time, code, now):
    """Scheduler calls only after a meaningful provider change, not on every poll."""
    from . import discussion

    if not config.watcher_participant_id or not config.conversation_enabled:
        return False
    if quiet(config, now):
        return False
    local = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata"))
    source = datetime.fromisoformat(source_time)
    if source.tzinfo is None:
        source = source.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    if not 0 <= now - source.timestamp() <= 1800 or type(code) is not int or not 0 <= code <= 99:
        return False
    eligible = [
        x["id"]
        for x in c.execute(select(db.items)).mappings()
        if datetime.fromisoformat(x["start"]).astimezone(ZoneInfo("Asia/Kolkata")).date()
        == local.date()
    ]
    if not eligible:
        return False
    event_id = f"watcher-weather:{source_time}:{code}"
    if c.execute(select(db.events.c.id).where(db.events.c.id == event_id)).first():
        return False
    last = c.execute(
        select(db.events.c.created_at)
        .where(db.events.c.kind == "watcher_event")
        .order_by(db.events.c.created_at.desc())
    ).scalar()
    if last is not None and now - last < 3600:
        return False
    identity = discussion.queue(
        c,
        Message(
            sender="operator-watcher",
            message_id=event_id,
            body="Assess the sourced current-weather event against the SIMULATED itinerary. "
            "Do not claim a forecast or modify anything.",
            timestamp=now * 1000,
        ),
        now,
    )
    if identity in {None, "busy"}:
        return False
    metadata = {
        "weather_event": {
            "status": "available",
            "source_url": "https://open-meteo.com/",
            "data": {"weather_code": code, "time": source_time},
        }
    }
    c.execute(
        update(db.discussions)
        .where(db.discussions.c.id == identity)
        .values(
            assigned_role="watcher",
            specialist_notes=json.dumps({"event_evidence": metadata, "eligible_items": eligible}),
        )
    )
    c.execute(
        insert(db.events).values(
            id=event_id,
            kind="watcher_event",
            payload=json.dumps({"discussion_id": identity}),
            status="queued",
            created_at=now,
        )
    )
    return True
