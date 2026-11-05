"""Deterministic travel features: no model decides hard constraints or arithmetic."""

import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update

from . import storage as db

KINDS = {
    "max_walk_m",
    "fatigue_limit",
    "vegetarian",
    "halal",
    "required_place",
    "wheelchair",
    "senior",
}


def set_preference(
    c, subject, kind, value, author, evidence_id, *, hard=True, strength=3, now=None
):
    now = int(time.time()) if now is None else now
    if kind not in KINDS or not 1 <= strength <= 5 or not subject or len(subject) > 80:
        raise ValueError("Invalid family preference")
    if kind in {"max_walk_m", "fatigue_limit"}:
        if type(value) is not int or not 0 <= value <= (20000 if kind == "max_walk_m" else 100):
            raise ValueError("Preference limit outside range")
    elif kind in {"vegetarian", "halal", "wheelchair", "senior"} and type(value) is not bool:
        raise ValueError("Preference must be true or false")
    elif kind == "required_place" and (not isinstance(value, str) or len(value) > 120):
        raise ValueError("Invalid required place")
    from .family import _reported_member, resolve

    member = resolve(c, author, "me" if subject == author else subject)
    if member:
        if member["participant_id"] and member["participant_id"] != author:
            raise ValueError("Only that linked member can confirm their preference")
        subject = member["id"]
    else:
        subject = _reported_member(c, author, subject, now)
    prior = (
        c.execute(
            select(db.preferences).where(
                db.preferences.c.subject == subject, db.preferences.c.kind == kind
            )
        )
        .mappings()
        .first()
    )
    if prior and prior["author"] != author:
        raise ValueError("Only the reporting person can correct this preference")
    values = {
        "value": json.dumps(value),
        "hard": hard,
        "strength": strength,
        "author": author,
        "evidence_id": evidence_id,
        "updated_at": now,
    }
    if prior:
        c.execute(update(db.preferences).where(db.preferences.c.id == prior["id"]).values(**values))
    else:
        c.execute(insert(db.preferences).values(subject=subject, kind=kind, **values))
    c.execute(
        insert(db.events).values(
            id="pref_" + secrets.token_hex(8),
            kind="preference_changed",
            payload=json.dumps(
                {
                    "subject": subject,
                    "kind": kind,
                    "before": dict(prior) if prior else None,
                    "after": values,
                }
            ),
            status="recorded",
            created_at=now,
        )
    )


def fatigue(walking_m, heat_c, stairs, travel_minutes):
    if any(x is None for x in [walking_m, heat_c, stairs, travel_minutes]):
        raise ValueError("Walking, heat, stairs and travel evidence are required")
    if not (
        0 <= walking_m <= 100000
        and -30 <= heat_c <= 60
        and 0 <= stairs <= 5000
        and 0 <= travel_minutes <= 1440
    ):
        raise ValueError("Invalid fatigue inputs")
    score = min(
        100, round(walking_m / 100 + max(0, heat_c - 28) * 2 + stairs / 10 + travel_minutes / 10)
    )
    return {
        "score": score,
        "bar": "â–ˆ" * (score // 10) + "â–‘" * (10 - score // 10),
        "label": "Planning comfort heuristic, not a medical or safety assessment",
    }


def rank_plans(plans, preferences):
    results = []
    for plan in plans:
        required = {
            "walking_m",
            "heat_c",
            "stairs",
            "travel_minutes",
            "places",
            "vegetarian",
            "halal",
            "wheelchair",
        }
        if required - set(plan):
            raise ValueError("Plan lacks constraint evidence")
        meter = (
            fatigue(plan["walking_m"], plan["heat_c"], plan["stairs"], plan["travel_minutes"])
            if all(plan[k] is not None for k in ["walking_m", "heat_c", "stairs", "travel_minutes"])
            else {
                "score": None,
                "bar": "unknown",
                "label": "Insufficient evidence for comfort score",
            }
        )
        scores, blockers, unknowns = {}, [], []
        for pref in preferences:
            subject, kind, target = pref["subject"], pref["kind"], json.loads(pref["value"])
            actual = {
                "max_walk_m": plan["walking_m"],
                "fatigue_limit": meter["score"],
                "vegetarian": plan["vegetarian"],
                "halal": plan["halal"],
                "wheelchair": plan["wheelchair"],
                "required_place": plan["places"],
                "senior": meter["score"] <= 40 if meter["score"] is not None else None,
            }.get(kind)
            passed = (
                actual is not None and actual <= target
                if kind in {"max_walk_m", "fatigue_limit"}
                else target.casefold() in [x.casefold() for x in actual]
                if kind == "required_place"
                else actual == target
            )
            if actual is None:
                unknowns.append(subject + ": " + kind)
                passed = False
            if (
                kind == "max_walk_m"
                and plan.get("onsite_walking_unknown")
                and passed
                and pref["hard"]
            ):
                passed = False
                unknowns.append(subject + ": on-site walking is unknown")
            if kind in {"vegetarian", "halal", "wheelchair", "senior"} and target is False:
                passed = True  # False disables a requirement; it does not require the opposite.
            scores.setdefault(subject, {"earned": 0, "possible": 0})
            scores[subject]["possible"] += pref["strength"]
            if passed:
                scores[subject]["earned"] += pref["strength"]
            elif pref["hard"]:
                blockers.append(subject + ": " + kind)
        results.append(
            {
                "name": plan.get("name", "Option"),
                "feasible": not blockers,
                "blockers": blockers,
                "unknowns": unknowns,
                "fatigue": meter,
                "scores": {s: round(100 * v["earned"] / v["possible"]) for s, v in scores.items()},
            }
        )
    return sorted(results, key=lambda p: (p["feasible"], sum(p["scores"].values())), reverse=True)


def demo_conflict(c):
    # Explicitly fictional route metrics. Live tool results must replace these
    # before a plan is presented as a verified real itinerary.
    from .family import preferences

    prefs = preferences(c)
    if not prefs:
        prefs = [
            {"subject": "Dad", "kind": "max_walk_m", "value": "800", "hard": True, "strength": 5},
            {
                "subject": "Kids",
                "kind": "required_place",
                "value": '"Charminar"',
                "hard": False,
                "strength": 4,
            },
            {"subject": "Mom", "kind": "vegetarian", "value": "true", "hard": True, "strength": 5},
        ]
    plans = [
        {
            "name": "SIMULATED long walking tour",
            "walking_m": 2400,
            "heat_c": 32,
            "stairs": 80,
            "travel_minutes": 30,
            "places": ["Charminar"],
            "vegetarian": True,
            "halal": None,
            "wheelchair": None,
        },
        {
            "name": "SIMULATED short visit + car transfers",
            "walking_m": 650,
            "heat_c": 29,
            "stairs": 0,
            "travel_minutes": 45,
            "places": ["Charminar"],
            "vegetarian": True,
            "halal": None,
            "wheelchair": None,
        },
    ]
    return rank_plans(plans, prefs)


def expense(c, sender, amount, description, participants, message_id, now):
    if not participants or len(set(participants)) != len(participants) or len(participants) > 12:
        raise ValueError("Choose one to twelve distinct split participants")
    minor = Decimal(amount) * 100
    if minor != minor.to_integral_value() or not 0 < minor <= 100000000:
        raise ValueError("Invalid INR expense amount")
    total = int(minor)
    shares = {
        p: total // len(participants) + (i < total % len(participants))
        for i, p in enumerate(participants)
    }
    c.execute(
        insert(db.expenses).values(
            id="expense_" + secrets.token_hex(8),
            message_id=message_id,
            payer=sender,
            amount_minor=total,
            description=description[:300],
            split=json.dumps(shares),
            created_at=now,
        )
    )
    return shares


def calendar(c, path=None, now=None):
    now = datetime.now(UTC) if now is None else now

    def stamp(value):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    def escape(value):
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Trippy//Travel Calendar//EN",
        "CALSCALE:GREGORIAN",
    ]
    for item in c.execute(select(db.items)).mappings():
        start = datetime.fromisoformat(item["start"])
        lines += [
            "BEGIN:VEVENT",
            f"UID:{item['trip_id']}-{item['id']}@trippy.local",
            "DTSTAMP:" + stamp(now),
            "DTSTART:" + stamp(start),
            "DTEND:" + stamp(start + timedelta(minutes=max(1, item["duration_minutes"]))),
            "SUMMARY:" + escape("SIMULATED " + item["id"]),
            "DESCRIPTION:" + escape("Demo itinerary; not a confirmed real reservation"),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    folded = []
    for line in lines:
        piece = ""
        for character in line:
            if len((piece + character).encode("utf-8")) > 74:
                folded.append(piece)
                piece = " "
            piece += character
        folded.append(piece)
    content = ("\r\n".join(folded) + "\r\n").encode("utf-8")
    if path is None:
        return content
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def handoffs(query="Hyderabad"):
    value = quote(query, safe="")
    return {
        "flights": "https://www.google.com/travel/flights?q=" + value,
        "hotels": "https://www.booking.com/searchresults.html?ss=" + value,
        "places": "https://www.openstreetmap.org/search?query=" + value,
        "note": (
            "Open these yourself to review availability and complete any booking; "
            "no payment is made here."
        ),
    }


def trigger_delay(c, config, event_id, minutes, now):
    from .service import TRIP, Coordinator, enqueue, planning_context, row

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", event_id) or not 1 <= minutes <= 360:
        raise ValueError("Use a stable event ID and a delay of 1â€“360 minutes")
    existing = c.execute(select(db.events).where(db.events.c.id == event_id)).mappings().first()
    payload = json.dumps(
        {"minutes": minutes, "source": "SIMULATED flight-status event"}, sort_keys=True
    )
    if existing:
        if existing["payload"] != payload:
            raise ValueError("Event ID was already used for different input")
        return False
    flight = row(c, db.items, "flight")
    arrival = datetime.fromisoformat(flight["start"]) + timedelta(minutes=minutes)
    for task in c.execute(
        select(db.tasks).where(
            ~db.tasks.c.state.in_(["completed", "failed", "partially_completed", "cancelled"])
        )
    ).mappings():
        c.execute(update(db.tasks).where(db.tasks.c.id == task["id"]).values(state="cancelled"))
        c.execute(
            update(db.outbox)
            .where(
                db.outbox.c.status == "pending",
                db.outbox.c.dedupe_key.in_([task["assignment_id"], task["proposal_id"]]),
            )
            .values(status="cancelled")
        )
    c.execute(update(db.items).where(db.items.c.id == "flight").values(start=arrival.isoformat()))
    trip = row(c, db.trips, TRIP)
    c.execute(update(db.trips).where(db.trips.c.id == TRIP).values(version=trip["version"] + 1))
    c.execute(
        insert(db.events).values(
            id=event_id, kind="flight_delay", payload=payload, status="processed", created_at=now
        )
    )
    affected = planning_context(c)["affected_ids"]
    # The Planner snapshot obtains the full recursive dependency closure.
    enqueue(
        c,
        event_id + ":alert",
        f"SIMULATED disruption: flight delayed {minutes} minutes. "
        f"Affected dependencies: {', '.join(affected)}. I'm preparing a repair "
        "for your approval; no reservations have changed.",
        now,
        show_reference=False,
    )
    Coordinator(None, config)._start(
        c,
        "SIMULATED proactive recovery after flight delay. "
        "Use late_checkin=true for the hotel and alternative=room_service for dinner if needed. "
        "Keep within the four-hour simulator exception windows.",
        now,
    )
    return bool(affected)


def local_tips():
    return (
        "Hyderabad planning tips: consider outdoor visits during cooler parts of the day; "
        "confirm the actual forecast. Ask each restaurant about vegetarian/halal needs. "
        "Keep time buffers around the prayer times your family provides; no live prayer-time "
        "source is connected. Senior-friendly mode should use explicit walking limits and "
        "verified seating/accessibility, with unknown tags flagged for confirmation."
    )


def chat_command(c, command, message, now):
    """Small explicit grammar: models never mutate shared constraints or money."""
    from .service import Coordinator

    if command == "help":
        return (
            "Tell me preferences naturally, e.g. 'I prefer vegetarian food', "
            "and confirm the suggestion.\n"
            "Send a travel image for review, or ask 'Show my plan as a visual'.\n"
            "trip family / trip visual / trip approvals\n"
            "Travel tools:\ntrip remember Dad max_walk_m 800 hard\n"
            "trip remember Mom vegetarian true hard\ntrip memory\n"
            "trip forget preference Dad max_walk_m\ntrip compare\n"
            "trip fatigue 650 29 0 45 (metres, C, stairs, travel minutes; your estimates)\n"
            "trip spent 1200 dinner | Dad,Mom,Me\ntrip expenses\n"
            "trip briefing 08:00 / trip briefing off\ntrip links Hyderabad\n"
            "trip tips / trip senior on\ntrip status / trip forget\n"
            "Approve/reject a proposal using its exact short code."
        )
    if command == "memory":
        from .family import preferences

        prefs = preferences(c)
        facts = [
            dict(p)
            for p in c.execute(
                select(db.facts).where(db.facts.c.sender == message.sender)
            ).mappings()
        ]
        return json.dumps(
            {"family_preferences": prefs, "your_conversation_memory": facts}, ensure_ascii=False
        )
    if command.startswith("remember "):
        parts = command.split(maxsplit=4)
        if len(parts) != 5:
            raise ValueError("Use: trip remember Dad max_walk_m 800 hard")
        _, subject, kind, rest = parts[0], parts[1], parts[2], parts[4]
        # Recombine the value and optional hard/soft marker, preserving place names.
        raw = parts[3] + " " + rest
        raw, marker = raw.rsplit(" ", 1)
        if marker not in {"hard", "soft"}:
            raise ValueError("End the preference with hard or soft")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        set_preference(
            c,
            subject,
            kind,
            value,
            message.sender,
            message.message_id,
            hard=marker == "hard",
            now=now,
        )
        return (
            f"Remembered {subject}: {kind} = {value} ({marker}). "
            "Reported by you; use the same command to correct it."
        )
    if command.startswith("forget preference "):
        from sqlalchemy import delete

        parts = command.split()
        if len(parts) != 4:
            raise ValueError("Use: trip forget preference Dad max_walk_m")
        from .family import resolve

        target = resolve(c, message.sender, parts[2])
        subject = target["id"] if target else parts[2]
        count = c.execute(
            delete(db.preferences).where(
                db.preferences.c.subject == subject,
                db.preferences.c.kind == parts[3],
                db.preferences.c.author == message.sender,
            )
        ).rowcount
        return f"Removed {count} preference(s) reported by you."
    if command == "compare":
        from .comparison import card

        latest = (
            c.execute(select(db.comparisons).order_by(db.comparisons.c.created_at.desc()).limit(1))
            .mappings()
            .first()
        )
        return (
            card(json.loads(latest["body"]))
            if latest
            else (
                "Tell me your starting place, main attraction and date. "
                "For example: Compare outings from my hotel near Charminar, "
                "visiting Charminar, on 6 November 2026."
            )
        )
    if command.startswith("fatigue "):
        try:
            result = fatigue(*[float(x) for x in command.split()[1:]])
        except (TypeError, ValueError) as exc:
            raise ValueError("Use: trip fatigue 650 29 0 45") from exc
        return "Based on your supplied estimates: " + json.dumps(result, ensure_ascii=False)
    if command.startswith("senior "):
        value = command[7:] == "on"
        if command[7:] not in {"on", "off"}:
            raise ValueError("Use: trip senior on or off")
        set_preference(
            c,
            message.sender,
            "senior",
            value,
            message.sender,
            message.message_id,
            hard=True,
            now=now,
        )
        return "Senior comfort preference updated. Confirm actual walking limits and accessibility."
    if command.startswith("spent "):
        try:
            entry, people = command[6:].split("|", 1)
            amount, description = entry.strip().split(" ", 1)
            participants = [x.strip() for x in people.split(",")]
            if any(not x or len(x) > 80 for x in participants):
                raise ValueError()
            shares = expense(
                c, message.sender, amount, description, participants, message.message_id, now
            )
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("Use: trip spent 1200 dinner | Dad,Mom,Me") from exc
        return "Recorded your reported expense. INR shares: " + ", ".join(
            f"{p}: {v / 100:.2f}" for p, v in shares.items()
        )
    if command == "expenses":
        rows = [dict(x) for x in c.execute(select(db.expenses).limit(50)).mappings()]
        return "Reported expenses (latest up to 50):\n" + json.dumps(rows, ensure_ascii=False)
    if command.startswith("briefing "):
        schedule = command[9:]
        if schedule == "off":
            schedule = ""
        elif not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule):
            raise ValueError("Use: trip briefing 08:00 or trip briefing off (India time)")
        Coordinator._state(c, "morning_schedule", schedule)
        state = "set for " + schedule + " India time" if schedule else "disabled"
        return f"Morning briefing {state}. The local worker must be running."
    if command == "tips":
        return local_tips()
    if command.startswith("links "):
        return json.dumps(handoffs(command[6:150]), ensure_ascii=False)
    return None


async def maybe_briefing(engine, config, weather_data, now):
    from .service import Coordinator, enqueue

    local = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata"))
    with engine.begin() as c:
        schedule = c.execute(
            select(db.state.c.value).where(db.state.c.key == "morning_schedule")
        ).scalar()
        if not schedule or local.strftime("%H:%M") < schedule:
            return False
        key = "briefing:" + local.date().isoformat()
        if c.execute(select(db.events.c.id).where(db.events.c.id == key)).first():
            return False
        c.execute(
            insert(db.events).values(
                id=key, kind="briefing", payload=schedule, status="queued", created_at=now
            )
        )
        today = [
            dict(x)
            for x in c.execute(select(db.items)).mappings()
            if datetime.fromisoformat(x["start"]).date() == local.date()
        ]
        body = "Morning briefing â€” Hyderabad\n"
        if weather_data.get("status") == "available":
            day = next(
                (d for d in weather_data["daily"] if d["date"] == local.date().isoformat()), None
            )
            if day:
                body += (
                    f"Forecast: {day['temperature_2m_min']}â€“{day['temperature_2m_max']} C; "
                    f"rain chance {day['precipitation_probability_max']}%. "
                    "Source: Open-Meteo (https://open-meteo.com/).\n"
                )
            else:
                body += "No forecast for today is available.\n"
        else:
            body += "Weather could not be checked.\n"
        body += "SIMULATED itinerary: " + (", ".join(x["id"] for x in today) or "no items today")
        body += "\nConsider water, comfortable footwear and any personal essentials."
        enqueue(c, key, body, now, show_reference=False)
        Coordinator._state(c, "last_briefing", key)
        return True
