"""Readable decision cards, rendered from the stored validated proposal."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import storage as db
from .contracts import PlanResult


def card(c, proposal, now):
    plan = PlanResult.model_validate_json(proposal["body"])
    task = c.execute(select(db.tasks).where(db.tasks.c.id == proposal["task_id"])).mappings().one()
    expiry = datetime.fromtimestamp(proposal["expires_at"], ZoneInfo("Asia/Kolkata"))
    lines = ["SIMULATED reservation proposal", "REVIEW CHANGES · no real bookings or payments", ""]
    original = c.execute(
        select(db.events.c.payload).where(db.events.c.id == proposal["id"] + ":before")
    ).scalar()
    snapshot = json.loads(original) if original else {}
    for change in plan.changes:
        old = snapshot.get(change.item_id)
        if old is None and task["state"] == "awaiting_approval":
            old = c.execute(
                select(db.items.c.start).where(db.items.c.id == change.item_id)
            ).scalar()
        before = datetime.fromisoformat(old).astimezone(ZoneInfo("Asia/Kolkata")) if old else None
        after = change.start.astimezone(ZoneInfo("Asia/Kolkata"))
        lines += [
            change.item_id.upper(),
            f"Was: {before:%d %b, %H:%M} IST"
            if before
            else "Original time unavailable for this older record",
            f"Proposed: {after:%d %b, %H:%M} IST",
            f"Extra: INR {change.extra_cost_minor / 100:.2f}",
        ]
        if change.late_checkin:
            lines.append("Alternative: simulated late check-in")
        if change.alternative == "room_service":
            lines.append("Alternative: simulated room service")
        lines.append("")
    lines += [
        f"TOTAL EXTRA: INR {sum(x.extra_cost_minor for x in plan.changes) / 100:.2f}",
        f"Expires: {expiry:%d %b %Y, %H:%M:%S} IST",
        f"Status: {task['state'].replace('_', ' ')}",
    ]
    if task["state"] == "awaiting_approval" and proposal["expires_at"] > now:
        code = proposal["approval_code"] or proposal["id"]
        lines += [
            "",
            "Send exactly one decision from your linked approver account:",
            f"approve {code}",
            f"reject {code}",
            "A casual yes does not approve this proposal.",
        ]
    lines += ["", "Reference: " + proposal["id"]]
    return "\n".join(lines)


def pending(c, now):
    proposals = (
        c.execute(
            select(db.proposals)
            .join(db.tasks, db.proposals.c.task_id == db.tasks.c.id)
            .where(db.tasks.c.state == "awaiting_approval")
        )
        .mappings()
        .all()
    )
    return (
        "\n\n".join(card(c, p, now) for p in proposals)
        if proposals
        else "There are no reservation proposals awaiting approval."
    )
