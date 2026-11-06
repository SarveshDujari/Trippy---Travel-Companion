"""Transparent memory cards and explicitly confirmed conversational utilities."""

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import storage as db


def stamp(value):
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M IST")


def card(c, sender, *, include_sensitive=False):
    from .privacy import SENSITIVE_KINDS

    people = {x["id"]: dict(x) for x in c.execute(select(db.family)).mappings()}
    names = {p["participant_id"]: p["name"] for p in people.values() if p["participant_id"]}
    lines = ["What I remember about your family"]
    hidden = 0
    for pref in c.execute(select(db.preferences).order_by(db.preferences.c.subject)).mappings():
        if pref["kind"] in SENSITIVE_KINDS and (not include_sensitive or pref["author"] != sender):
            hidden += 1
            continue
        person = people.get(pref["subject"], {})
        lines += [
            f"• {person.get('name', 'Reported companion')}: "
            f"{pref['kind'].replace('_', ' ')} = {json.loads(pref['value'])} "
            f"({'hard limit' if pref['hard'] else 'preference'})",
            f"  Reported by {names.get(pref['author'], 'linked family member')} · "
            f"{stamp(pref['updated_at'])} · evidence: {pref['evidence_id']}",
        ]
    for fact in c.execute(select(db.facts).where(db.facts.c.sender == sender)).mappings():
        from .privacy import SENSITIVE

        if not include_sensitive and (fact["key"] == "mobility" or SENSITIVE.search(fact["value"])):
            hidden += 1
            continue
        lines += [
            f"• Your {fact['key']}: {fact['value']}",
            f"  You · {stamp(fact['updated_at'])} · evidence: {fact['source_message_id']}",
        ]
    if len(lines) == 1:
        lines.append("No shareable preferences or explicit facts saved yet.")
    if hidden:
        lines.append(
            "Personal mobility/health details are hidden. Request explicitly with "
            "'Share with group:' to show them here."
        )
    lines.append(
        "Correct a fact naturally, or ask to forget your memory. Names do not grant authority."
    )
    return "\n".join(lines)


def propose_expense(c, draft, current, now):
    from .family import _create, _evidence
    from .grounding import numbers

    _evidence(draft.evidence, current["request"])
    if draft.description.casefold() not in draft.evidence.casefold():
        raise ValueError("Expense description must appear in the current message")
    if Decimal(draft.amount) not in numbers(draft.evidence):
        raise ValueError("Expense amount must appear in the current message")
    if len({x.strip().casefold() for x in draft.participants}) != len(draft.participants) or any(
        not x.strip() or len(x) > 80 or x.casefold() not in draft.evidence.casefold()
        for x in draft.participants
    ):
        raise ValueError("Name every split participant explicitly; do not guess")
    if not 0 < Decimal(draft.amount) <= 1000000:
        raise ValueError("Expense amount outside allowed range")
    code = _create(
        c,
        "expense",
        current["sender"],
        current["sender"],
        current["message_id"],
        draft.model_dump(),
        now,
    )
    return (
        f"Record reported expense: INR {draft.amount}, {draft.description}; equal split "
        f"among {', '.join(draft.participants)}?\nyes {code} / no {code}\n"
        "Expires in 10 minutes. This records information; no money moves."
    )


def propose_forget(c, current, now):
    from .family import _create

    code = _create(
        c, "forget", current["sender"], current["sender"], current["message_id"], {}, now
    )
    return (
        f"Clear your local discussion history, explicit facts and preferences reported "
        f"by you? Booking/expense audits and external chats remain.\nyes {code} / no {code}"
    )
