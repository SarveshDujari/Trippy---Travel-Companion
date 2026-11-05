"""Bounded conversation workflow and evidence-backed user memory."""

import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select, update

from . import storage as db
from .grounding import verify


def queue(c, message, now):
    if not message.body.strip():
        return None
    recent = c.execute(
        select(func.count())
        .select_from(db.discussions)
        .where(db.discussions.c.sender == message.sender, db.discussions.c.created_at >= now - 60)
    ).scalar()
    pending = c.execute(
        select(func.count())
        .select_from(db.discussions)
        .where(db.discussions.c.status.in_(["queued", "waiting", "tool_pending"]))
    ).scalar()
    if recent >= 8 or pending >= 10:
        return "busy"
    identity = "discussion_" + uuid4().hex[:12]
    c.execute(
        insert(db.discussions).values(
            id=identity,
            message_id=message.message_id,
            sender=message.sender,
            request=message.body[:4000],
            status="queued",
            created_at=now,
            request_time=message.timestamp // 1000,
        )
    )
    c.execute(
        insert(db.chat).values(
            discussion_id=identity, role="human", body=message.body[:4000], created_at=now
        )
    )
    return identity


def context(c, discussion):
    eligible = select(db.discussions.c.id).where(
        (db.discussions.c.status == "completed") | (db.discussions.c.id == discussion["id"])
    )
    history = (
        c.execute(
            select(
                db.chat.c.role,
                db.chat.c.body,
                db.discussions.c.sender,
                db.discussions.c.request_time,
            )
            .join(db.discussions, db.chat.c.discussion_id == db.discussions.c.id)
            .where(db.chat.c.discussion_id.in_(eligible))
            .order_by(db.chat.c.id.desc())
            .limit(12)
        )
        .mappings()
        .all()
    )
    memory = (
        c.execute(
            select(db.facts.c.key, db.facts.c.value).where(
                db.facts.c.sender == discussion["sender"]
            )
        )
        .mappings()
        .all()
    )
    bounded_history, characters = [], 0
    for message in history:
        if characters + len(message["body"]) > 10000:
            break
        bounded_history.append(dict(message))
        characters += len(message["body"])
    return {
        "conversation": list(reversed(bounded_history)),
        "this_persons_explicit_trip_details": [dict(f) for f in memory],
        "default_destination": "Hyderabad, India",
        "real_itinerary_status": "Not imported. Do not treat demo reservations as real bookings.",
        "pending_demo_booking_approvals": [
            {
                "proposal_id": t["proposal_id"],
                "mode": "SIMULATED",
                "approval_command": "trip approve " + t["proposal_id"],
            }
            for t in c.execute(
                select(db.tasks).where(db.tasks.c.state == "awaiting_approval")
            ).mappings()
        ],
    }


def accept(c, reply, now):
    current = (
        c.execute(select(db.discussions).where(db.discussions.c.id == reply.discussion_id))
        .mappings()
        .first()
    )
    if (
        current is None
        or current["status"] != "waiting"
        or current["assignment_id"] != reply.assignment_id
        or current["deadline"] < now
    ):
        return False
    if any(f.evidence != current["request"] or f.value != f.evidence for f in reply.facts):
        raise ValueError("Travel memory must quote this person's actual message")
    snapshot = json.loads(current["context"])
    if reply.uses_weather and snapshot["weather"]["status"] != "available":
        raise ValueError("A weather answer requires available forecast evidence")
    rendered = verify(reply, snapshot, current["request"])
    for fact in reply.facts:
        condition = (db.facts.c.sender == current["sender"]) & (db.facts.c.key == fact.key)
        values = {
            "value": fact.value,
            "evidence": fact.evidence,
            "source_message_id": current["message_id"],
            "updated_at": now,
        }
        if c.execute(select(db.facts.c.key).where(condition)).first():
            c.execute(update(db.facts).where(condition).values(**values))
        else:
            c.execute(insert(db.facts).values(sender=current["sender"], key=fact.key, **values))
    c.execute(
        insert(db.chat).values(
            discussion_id=reply.discussion_id,
            role="assistant",
            body=reply.reply + ("\n" + rendered if rendered else ""),
            created_at=now,
        )
    )
    c.execute(
        update(db.discussions)
        .where(db.discussions.c.id == reply.discussion_id)
        .values(status="completed")
    )
    return True


def forget(c, sender):
    ids = select(db.discussions.c.id).where(db.discussions.c.sender == sender)
    identities = list(c.execute(ids).scalars())
    assignments = list(
        c.execute(
            select(db.discussions.c.assignment_id).where(db.discussions.c.sender == sender)
        ).scalars()
    )
    related = db.outbox.c.related_id.in_(identities) | db.outbox.c.dedupe_key.in_(
        [x for x in assignments if x] + [x + ":reply" for x in identities]
    )
    c.execute(
        update(db.outbox)
        .where(related, db.outbox.c.status.in_(["pending", "unknown"]))
        .values(status="cancelled")
    )
    c.execute(update(db.outbox).where(related).values(body="[forgotten context]"))
    c.execute(delete(db.tools).where(db.tools.c.discussion_id.in_(ids)))
    c.execute(delete(db.chat).where(db.chat.c.discussion_id.in_(ids)))
    c.execute(delete(db.discussions).where(db.discussions.c.sender == sender))
    c.execute(delete(db.facts).where(db.facts.c.sender == sender))
    c.execute(delete(db.preferences).where(db.preferences.c.author == sender))
    for audit in c.execute(
        select(db.events).where(db.events.c.kind == "preference_changed")
    ).mappings():
        if json.loads(audit["payload"])["after"]["author"] == sender:
            c.execute(delete(db.events).where(db.events.c.id == audit["id"]))


def dispatch(c, discussion, config, forecast, now):
    # Import at use time to keep service/discussion modules independently reusable.
    from .service import enqueue

    snapshot = context(c, discussion)
    snapshot["weather"] = forecast
    evidence = {}
    for job in c.execute(
        select(db.tools).where(
            db.tools.c.discussion_id == discussion["id"], db.tools.c.status == "done"
        )
    ).mappings():
        evidence[job["id"]] = json.loads(job["result"])
        if job["name"] == "weather" and evidence[job["id"]]["status"] == "available":
            snapshot["weather"] = evidence[job["id"]]["data"]
    snapshot["evidence"] = evidence
    snapshot["tool_calls_remaining"] = max(
        0,
        3
        - c.execute(
            select(func.count())
            .select_from(db.tools)
            .where(db.tools.c.discussion_id == discussion["id"])
        ).scalar(),
    )
    snapshot["family_preferences"] = [dict(p) for p in c.execute(select(db.preferences)).mappings()]
    snapshot["current_time"] = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).isoformat()
    snapshot["request_time"] = datetime.fromtimestamp(
        discussion.get("request_time") or discussion["created_at"], ZoneInfo("Asia/Kolkata")
    ).isoformat()
    assignment = "assignment_" + uuid4().hex[:12]
    payload = {
        "discussion_id": discussion["id"],
        "assignment_id": assignment,
        "request": discussion["request"],
        "context": snapshot,
    }
    c.execute(
        update(db.discussions)
        .where(db.discussions.c.id == discussion["id"])
        .values(
            status="waiting",
            assignment_id=assignment,
            deadline=now + min(120, config.assignment_ttl_seconds),
            context=json.dumps(snapshot),
        )
    )
    enqueue(
        c,
        assignment,
        f"@{config.planner_name} DISCUSSION_ASSIGNMENT\n" + json.dumps(payload),
        now,
        room_id=config.work_room_id or config.room_id,
        related_id=discussion["id"],
    )


def request_tool(c, request, now):
    from .travel_tools import validate_request

    current = (
        c.execute(select(db.discussions).where(db.discussions.c.id == request.discussion_id))
        .mappings()
        .first()
    )
    if (
        not current
        or current["status"] != "waiting"
        or current["assignment_id"] != request.assignment_id
        or current["deadline"] < now
    ):
        raise ValueError("Tool request belongs to an inactive discussion")
    count = c.execute(
        select(func.count())
        .select_from(db.tools)
        .where(db.tools.c.discussion_id == request.discussion_id)
    ).scalar()
    if count >= 3:
        raise ValueError("Tool budget exhausted")
    validate_request(request.tool, request.arguments)
    c.execute(
        insert(db.tools).values(
            id="evidence_" + uuid4().hex[:12],
            discussion_id=request.discussion_id,
            name=request.tool,
            arguments=json.dumps(request.arguments),
            status="queued",
            created_at=now,
        )
    )
    c.execute(
        update(db.discussions)
        .where(db.discussions.c.id == request.discussion_id)
        .values(status="tool_pending")
    )
