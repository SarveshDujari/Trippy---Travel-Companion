"""Quiet, contextual trip utilities over user-confirmed information.

No supplier actions or inferred bookings. Personal utilities use sender-scoped
state; itinerary edits reuse expiring, identity-bound confirmation records.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update

from . import storage as db

TZ = ZoneInfo("Asia/Kolkata")
FIXED = {"flight", "train", "hotel", "transfer", "other"}
CHECKS = {"tickets": "Tickets reviewed", "stay": "Stay details reviewed", "packing": "Packed"}


def state_key(sender):
    return "companion:" + hashlib.sha256(sender.encode()).hexdigest()


def read(c, sender):
    raw = c.execute(select(db.state.c.value).where(db.state.c.key == state_key(sender))).scalar()
    return json.loads(raw) if raw else {}


def save(c, sender, data):
    from .service import Coordinator

    Coordinator._state(c, state_key(sender), json.dumps(data))


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(TZ) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def stops(c, sender):
    return sorted(
        [
            dict(x)
            for x in c.execute(
                select(db.imported).where(
                    db.imported.c.reported_by == sender, db.imported.c.status != "skipped"
                )
            ).mappings()
        ],
        key=lambda x: (parse_time(x["start"]) or datetime.max.replace(tzinfo=TZ), x["id"]),
    )


def upcoming(c, sender, now):
    at = datetime.fromtimestamp(now, TZ)
    return [
        x
        for x in stops(c, sender)
        if parse_time(x["start"])
        and (parse_time(x["end"]) or parse_time(x["start"])) > at
        and not (x["kind"] == "hotel" and parse_time(x["start"]) <= at)
    ]


def display(value):
    # Do not repeat incidental health/expense details in a compact group card.
    from .privacy import SENSITIVE

    return "Personal trip item" if SENSITIVE.search(value or "") else value or "Not supplied"


def map_link(item):
    location = item.get("location")
    if not location or display(location) == "Personal trip item":
        return "Location missing; add it before requesting directions."
    return "Map: https://www.google.com/maps/search/?api=1&query=" + quote(location, safe="")


def clock_label(value):
    parsed = parse_time(value)
    return parsed.strftime("%d %b %H:%M IST") if parsed else "time not supplied"


def next_card(c, sender, now):
    items = upcoming(c, sender, now)
    if not items:
        return "No upcoming timed stops saved. Send your itinerary for review, or use trip add."
    item = items[0]
    at = datetime.fromtimestamp(now, TZ)
    label = "In progress" if parse_time(item["start"]) <= at else "Next"
    lines = [f"{label}: {display(item['title'])} · {clock_label(item['start'])}", map_link(item)]
    minutes = read(c, sender).get("travel_minutes", {}).get(item["id"])
    if minutes is not None:
        leave = parse_time(item["start"]) - timedelta(minutes=minutes)
        lines.append(f"Leave by {leave:%H:%M IST} using your {minutes}-minute travel allowance.")
    else:
        lines.append(
            "Departure time needs your starting point and travel time; it isn't estimated yet."
        )
    hint = weather_hint(c, sender, item, now)
    if hint:
        lines.append(hint)
    lines += [
        "Saved trip information; not supplier-verified.",
        "Reply: show route · find food · remind me",
    ]
    return "\n".join(lines)


def gap_hints(items):
    """At most two prompts, only on a long scheduled day; never invent meals."""
    timed = [x for x in items if parse_time(x["start"]) and parse_time(x["end"])]
    if len(timed) < 2:
        return []
    first, last = (
        min(parse_time(x["start"]) for x in timed),
        max(parse_time(x["end"]) for x in timed),
    )
    if (last - first).total_seconds() < 4 * 3600:
        return []
    hints = []
    if first.hour <= 12 and last.hour >= 14 and not any(x["kind"] == "meal" for x in timed):
        hints.append("No meal is recorded across lunchtime. Would you like to plan a food stop?")
    # Only warn about a continuous busy stretch, not a day with a large free gap.
    busy_start, busy_end = first, first
    longest = 0
    for item in sorted(timed, key=lambda x: parse_time(x["start"])):
        start, end = parse_time(item["start"]), parse_time(item["end"])
        if item["kind"] in {"meal", "break", "hotel"} or start - busy_end >= timedelta(minutes=30):
            busy_start = end if item["kind"] in {"meal", "break", "hotel"} else start
        busy_end = max(busy_end, end)
        longest = max(longest, (busy_end - busy_start).total_seconds())
    if longest >= 4 * 3600:
        hints.append("A long scheduled stretch has no recorded rest. Consider a short break.")
    return hints[:2]


def weather_hint(c, sender, item, now, weather=None):
    """Only explicitly outdoor Hyderabad stops with fresh, date-matched bad weather."""
    flags = read(c, sender).get("flags", {}).get(item["id"], {})
    demo = c.execute(select(db.state.c.value).where(db.state.c.key == "cached-demo-mode")).scalar()
    if demo == "true":
        return None
    if not flags.get("outdoor") or "hyderabad" not in (item.get("location") or "").casefold():
        return None
    if weather is None:
        raw = c.execute(
            select(db.state.c.value).where(db.state.c.key == "forecast_hyderabad")
        ).scalar()
        weather = json.loads(raw) if raw else {}
    start = parse_time(item["start"])
    end = parse_time(item["end"]) or start
    if not end or end.timestamp() <= now:
        return None
    age = now - weather.get("fetched_at", 0)
    if (
        not start
        or weather.get("status") != "available"
        or weather.get("demo_mode")
        or not 0 <= age <= 900
        or "hyderabad" not in weather.get("location", "").casefold()
    ):
        return None
    day = next(
        (x for x in weather.get("daily", []) if x.get("date") == start.date().isoformat()), {}
    )
    rain, heat = day.get("precipitation_probability_max"), day.get("temperature_2m_max")
    reasons = []
    if type(rain) in {int, float} and rain >= 60:
        reasons.append(f"{rain:g}% daily rain chance")
    if type(heat) in {int, float} and heat >= 35 and 11 <= start.hour < 17:
        reasons.append(f"{heat:g} C daily high")
    if not reasons:
        return None
    return (
        f"For {display(item['title'])}: consider an indoor alternative because of "
        + " and ".join(reasons)
        + ". This is a daily forecast, not conditions at the exact stop time. "
        "Ask for indoor options nearby; availability still needs checking. "
        f"Source: https://open-meteo.com/ · checked {weather.get('checked_at', 'recently')}."
    )


def checklist(c, sender, now):
    data, items = read(c, sender), stops(c, sender)
    checks = data.get("checks", {})
    lines = ["Your trip readiness"]
    for key, label in CHECKS.items():
        lines.append(f"{'Done' if checks.get(key) else 'To do'}: {label} [{key}]")
    if not items:
        lines.append("To do: add your real itinerary.")
    elif any(not x["start"] or not x["location"] for x in items):
        lines.append("To do: fill missing stop times or locations.")
    pending = (
        c.execute(
            select(db.confirmations.c.code).where(
                db.confirmations.c.confirmer == sender,
                db.confirmations.c.status == "pending",
                db.confirmations.c.expires_at > now,
            )
        )
        .scalars()
        .all()
    )
    if pending:
        lines.append(
            "Pending review codes: "
            + ", ".join(pending[:5])
            + ". Use the original preview to decide."
        )
    lines.append("Update: trip done packing (or trip undo packing). Checks are self-reported.")
    return "\n".join(lines)


def summary(c, sender, now):
    items = stops(c, sender)
    if not items:
        return "No real itinerary saved yet. Send a ticket image for review, or use trip add."
    lines = ["Your trip at a glance · saved information, not supplier-verified"]
    for item in items[:12]:
        lines.append(
            f"• {clock_label(item['start'])}: {display(item['title'])} "
            f"({item['kind']}) [{item['id']}]"
        )
        if item["location"]:
            lines.append("  " + map_link(item))
    if len(items) > 12:
        lines.append(
            f"Showing the first 12 of {len(items)} stops. Ask what's next for the current stop."
        )
    today = datetime.fromtimestamp(now, TZ).date()
    lines += gap_hints(
        [x for x in items if parse_time(x["start"]) and parse_time(x["start"]).date() == today]
    )
    hint = next(
        (h for x in upcoming(c, sender, now) if (h := weather_hint(c, sender, x, now))), None
    )
    if hint:
        lines.append(hint)
    lines.append("Reply: what's next · checklist")
    return "\n".join(lines)


def budget(c, sender, now):
    data = read(c, sender)
    limit = data.get("daily_budget_minor")
    if limit is None:
        return (
            "Set your daily INR budget with trip budget 1500. "
            "It compares your own recorded split shares."
        )
    today = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = 0
    # Only sender-reported records: do not silently attribute someone else's bill.
    from .family import member_id

    names = {"me", "myself", sender.casefold(), member_id(sender).casefold()}
    person = (
        c.execute(select(db.family).where(db.family.c.participant_id == sender)).mappings().first()
    )
    if person:
        names.add(person["name"].casefold())
    unassigned = 0
    for row in c.execute(
        select(db.expenses).where(
            db.expenses.c.payer == sender,
            db.expenses.c.created_at >= int(today.timestamp()),
            db.expenses.c.created_at < int((today + timedelta(days=1)).timestamp()),
        )
    ).mappings():
        shares = [v for k, v in json.loads(row["split"]).items() if k.casefold() in names]
        if len(shares) == 1:
            spent += shares[0]
        else:
            unassigned += 1
    difference = limit - spent
    return (
        f"Today (IST): budget INR {limit / 100:.2f} · "
        f"recorded personal shares INR {spent / 100:.2f}\n"
        + (
            f"Remaining: INR {difference / 100:.2f}"
            if difference >= 0
            else f"Over budget: INR {-difference / 100:.2f}"
        )
        + "\nBased only on expenses you recorded today; unrecorded spending is excluded."
        + (f" {unassigned} bill(s) excluded because your share is ambiguous." if unassigned else "")
    )


def late_card(c, sender, now, minutes):
    items = [
        x
        for x in upcoming(c, sender, now)
        if parse_time(x["start"]).date() == datetime.fromtimestamp(now, TZ).date()
    ]
    if not items:
        return "No upcoming timed stops today. Save your itinerary before adjusting it."
    data = read(c, sender)
    lines = [f"Preview for a {minutes}-minute delay — nothing changed."]
    for item in items[:6]:
        fixed = item["kind"] in FIXED or data.get("flags", {}).get(item["id"], {}).get(
            "fixed", True
        )
        if fixed:
            lines.append(
                f"Keep {display(item['title'])} at {clock_label(item['start'])}; treat it as fixed."
            )
        else:
            lines.append(
                f"Consider shortening or skipping {display(item['title'])} [{item['id']}]."
            )
    lines.append(
        "Travel buffers are not verified. Fixed bookings stay unchanged; "
        "contact the provider if you may miss one."
    )
    lines.append(
        "For an explicitly flexible stop, trip skip ID shows a separate confirmation preview."
    )
    return "\n".join(lines)


def propose(c, sender, message_id, payload, description, now):
    from .family import _create

    code = _create(c, "companion", sender, sender, message_id, payload, now)
    return (
        f"Preview — {description}\nNo saved itinerary change yet. yes {code} / no {code}\n"
        "Expires in 10 minutes. No supplier booking is changed."
    )


def apply_edit(c, sender, payload):
    from sqlalchemy import insert

    if payload["operation"] == "add":
        c.execute(
            insert(db.imported).values(
                id=payload["code"] + ":0",
                confirmation_code=payload["code"],
                reported_by=sender,
                source_message_id=payload["message_id"],
                status="user_confirmed_information_not_supplier_verified",
                **payload["item"],
            )
        )
        return
    current = (
        c.execute(
            select(db.imported).where(
                db.imported.c.id == payload["id"],
                db.imported.c.reported_by == sender,
            )
        )
        .mappings()
        .first()
    )
    if not current or dict(current) != payload["expected"]:
        raise ValueError("This stop changed since the preview. Request a fresh preview.")
    data = read(c, sender)
    if data.get("flags", {}).get(payload["id"], {}) != payload["expected_flags"]:
        raise ValueError("This stop's settings changed. Request a fresh preview.")
    if payload["operation"] == "flags":
        data.setdefault("flags", {})[payload["id"]] = payload["flags"]
        save(c, sender, data)
    else:
        changes = payload["changes"]
        if "start" in changes:
            start, finish = parse_time(changes["start"]), parse_time(changes["end"])
            for other in stops(c, sender):
                a, b = parse_time(other["start"]), parse_time(other["end"])
                if other["id"] != payload["id"] and a and b and start < b and finish > a:
                    raise ValueError(
                        "Another stop now overlaps this preview. Request a fresh time."
                    )
        c.execute(
            update(db.imported)
            .where(db.imported.c.id == payload["id"])
            .values(**payload["changes"])
        )
        if data.get("reminder", {}).get("id") == payload["id"]:
            data.pop("reminder", None)
            save(c, sender, data)


ALIASES = {
    "what's next": "next",
    "what’s next": "next",
    "what is next": "next",
    "show route": "route",
    "trip summary": "summary",
    "show my trip": "summary",
    "checklist": "checklist",
    "trip readiness": "checklist",
    "remind me": "remind",
    "skip this stop": "skip next",
    "daily budget": "budget",
    "budget remaining": "budget",
    "end of day check-in": "checkin",
    "today was tiring": "checkin tired",
    "today was good": "checkin good",
    "why this recommendation": "why",
}


def command(c, text, sender, message_id, now):
    """Exact shortcuts work without a model. Unknown prose remains a discussion."""
    normalized = text.strip().rstrip("?!.").casefold()
    raw = (
        text.strip()[5:].strip()
        if text.strip().casefold().startswith("trip ")
        else ALIASES.get(normalized)
    )
    match = re.fullmatch(
        r"(?:we(?:'re| are)|i(?:'m| am)) (?:running )?(\d{1,3}) minutes late", normalized
    )
    if match:
        raw = "late " + match[1]
    if raw is None:
        return None
    action, _, arg = raw.partition(" ")
    action, arg = action.casefold(), arg.strip()
    data = read(c, sender)
    if action == "next":
        return next_card(c, sender, now)
    if action == "summary":
        return summary(c, sender, now)
    if action == "route":
        items = upcoming(c, sender, now)
        return map_link(items[0]) if items else "No upcoming stop with a saved location."
    if action == "checklist":
        return checklist(c, sender, now)
    if action in {"done", "undo"}:
        if action == "undo" and re.fullmatch(r"P[A-Fa-f0-9]{6}", arg):
            return None
        if arg not in CHECKS:
            raise ValueError(
                "Choose tickets, stay or packing: trip done packing / trip undo packing"
            )
        data.setdefault("checks", {})[arg] = action == "done"
        save(c, sender, data)
        return checklist(c, sender, now)
    if action == "budget":
        if arg:
            try:
                amount = Decimal(arg) * 100
                if (
                    not amount.is_finite()
                    or amount != amount.to_integral_value()
                    or not 0 < amount <= 100000000
                ):
                    raise ValueError()
            except (InvalidOperation, ValueError):
                raise ValueError(
                    "Use trip budget 1500 with a positive INR amount, at most two decimal places."
                ) from None
            data["daily_budget_minor"] = int(amount)
            save(c, sender, data)
        return budget(c, sender, now)
    if action == "late":
        if not arg.isdigit() or not 1 <= int(arg) <= 360:
            return (
                "How many minutes late? For example: trip late 20. This only previews adjustments."
            )
        return late_card(c, sender, now, int(arg))
    if action == "checkin":
        if arg in {"tired", "good", "busy"}:
            data["feedback"] = {
                "day": datetime.fromtimestamp(now, TZ).date().isoformat(),
                "pace": arg,
            }
            save(c, sender, data)
            return (
                "Saved for tomorrow: suggest fewer optional stops and a rest break. "
                "Your itinerary is unchanged."
                if arg in {"tired", "busy"}
                else "Saved: today's pace worked for you. Your itinerary is unchanged."
            )
        if arg == "off" or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", arg):
            if arg != "off" and not 7 <= int(arg[:2]) < 22:
                raise ValueError("Choose a check-in time between 07:00 and 21:59 IST.")
            data["checkin_time"] = "" if arg == "off" else arg
            save(c, sender, data)
            return "Evening check-in " + (
                "off."
                if arg == "off"
                else f"set for {arg} IST in this group. "
                "Quiet hours: 22:00–07:00; worker must be online."
            )
        return (
            "How was today's pace? Reply trip checkin tired / good / busy. "
            "Optional daily prompt: trip checkin 20:30; stop with trip checkin off."
        )
    if action == "remind":
        if arg == "off":
            data.pop("reminder", None)
            save(c, sender, data)
            return "Your next-stop reminder is off."
        items = [x for x in upcoming(c, sender, now) if parse_time(x["start"]).timestamp() > now]
        if not items:
            return "Save an upcoming timed stop first."
        if arg and (not arg.isdigit() or not 1 <= int(arg) <= 180):
            raise ValueError(
                "Use trip remind 30 (minutes before the next stop), or trip remind off."
            )
        lead = int(arg or "30")
        item = items[0]
        due = int(parse_time(item["start"]).timestamp()) - lead * 60
        data["reminder"] = {
            "id": item["id"],
            "start": item["start"],
            "due": max(now, due),
            "sent": False,
            "key": hashlib.sha256(message_id.encode()).hexdigest()[:24],
        }
        save(c, sender, data)
        return (
            f"Reminder set for your next stop, {lead} minutes before its start "
            "(or shortly if already due). It appears here; quiet hours 22:00–07:00. "
            "Worker must be online."
        )
    if action == "travel":
        parts = arg.split()
        if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 720:
            raise ValueError(
                "Use trip travel STOP_ID 30 to save your estimated travel allowance in minutes."
            )
        if not any(x["id"] == parts[0] for x in stops(c, sender)):
            raise ValueError("Choose one of your saved stop IDs from trip summary.")
        data.setdefault("travel_minutes", {})[parts[0]] = int(parts[1])
        save(c, sender, data)
        return "Saved your travel allowance. What's next will use it to calculate departure time."
    if action == "add":
        if not arg:
            return (
                "Add a stop: trip add Title | activity "
                "(or meal, break, flight, train, hotel, transfer) | "
                "start ISO datetime with timezone | end ISO datetime with timezone | location. "
                "You'll review before saving."
            )
        from .contracts import ImportItem

        parts = [x.strip() for x in arg.split("|")]
        if len(parts) != 5:
            raise ValueError(
                "Use trip add Title | kind | start ISO datetime with timezone | "
                "end ISO datetime with timezone | location"
            )
        item = ImportItem(
            title=parts[0], kind=parts[1], start=parts[2], end=parts[3], location=parts[4]
        )
        payload = {
            "operation": "add",
            "item": item.model_dump(mode="json"),
            "message_id": message_id,
        }
        return propose(
            c,
            sender,
            message_id,
            payload,
            f"add {display(item.title)}, {clock_label(parts[2])} to {clock_label(parts[3])}, "
            f"{display(item.location)}",
            now,
        )
    if action in {"skip", "move", "mark"}:
        identity, _, value = arg.partition(" ")
        if identity == "next":
            items = upcoming(c, sender, now)
            identity = items[0]["id"] if items else ""
        item = next((x for x in stops(c, sender) if x["id"] == identity), None)
        if not item:
            raise ValueError("Choose one of your saved stop IDs from trip summary.")
        flags = data.get("flags", {}).get(identity, {})
        payload = {"id": identity, "expected": item, "expected_flags": flags}
        if action == "mark":
            if value not in {"outdoor", "indoor", "fixed", "flexible"}:
                raise ValueError("Use trip mark STOP_ID outdoor / indoor / fixed / flexible.")
            if value == "flexible" and item["kind"] in FIXED:
                raise ValueError("Transport, stays and unknown booking types remain fixed.")
            change = (
                {"outdoor": value == "outdoor"}
                if value in {"outdoor", "indoor"}
                else {"fixed": value == "fixed"}
            )
            payload.update(operation="flags", flags={**flags, **change})
            description = f"mark {display(item['title'])} as {value}"
        else:
            if item["kind"] in FIXED or flags.get("fixed", True):
                raise ValueError(
                    "This stop is fixed. Only an activity explicitly marked flexible "
                    "can be moved or skipped."
                )
            changes = {"status": "skipped"}
            description = f"skip {display(item['title'])} at {clock_label(item['start'])}"
            if action == "move":
                start, old, end = (
                    parse_time(value),
                    parse_time(item["start"]),
                    parse_time(item["end"]),
                )
                if not start or not old or not end:
                    raise ValueError(
                        "Move needs a timezone-aware new start and an existing start/end."
                    )
                finish = start + (end - old)
                for other in stops(c, sender):
                    a, b = parse_time(other["start"]), parse_time(other["end"])
                    if other["id"] != identity and a and b and start < b and finish > a:
                        raise ValueError(
                            "The new time overlaps another saved stop. Choose a different time."
                        )
                changes = {"start": start.isoformat(), "end": finish.isoformat()}
                description = (
                    f"move {display(item['title'])}: {clock_label(item['start'])} "
                    f"→ {clock_label(value)}; keep its duration. "
                    "Travel buffers still need checking"
                )
            payload.update(operation="edit", changes=changes)
        return propose(c, sender, message_id, payload, description, now)
    if action == "why":
        from .comparison import card

        row = (
            c.execute(
                select(db.comparisons)
                .where(db.comparisons.c.sender == sender)
                .order_by(db.comparisons.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if not row:
            return (
                "No saved comparison to explain yet. "
                "Ask me to compare two outings using your preferences."
            )
        body = json.loads(row["body"])
        # A stored comparison may predate the current group-privacy setting.
        for plan in body["plans"]:
            result = plan["evaluation"]
            result["scores"] = {}
            result["privacy_redacted"] = True
            if result["blockers"]:
                result["blockers"] = ["A hard requirement is unmet or unverified"]
            if result["unknowns"]:
                result["unknowns"] = ["Some requirements still need checking"]
        return card(body)
    return None


def tick(c, now):
    """Durable dedupe, no missed-stop catch-up, one generic evening prompt per day."""
    from .service import enqueue

    local = datetime.fromtimestamp(now, TZ)
    if local.hour < 7 or local.hour >= 22:
        return
    for row in (
        c.execute(select(db.state).where(db.state.c.key.startswith("companion:"))).mappings().all()
    ):
        data = json.loads(row["value"])
        reminder = data.get("reminder")
        if reminder and not reminder["sent"] and now >= reminder["due"]:
            item = (
                c.execute(select(db.imported).where(db.imported.c.id == reminder["id"]))
                .mappings()
                .first()
            )
            if (
                item
                and item["status"] != "skipped"
                and item["start"] == reminder["start"]
                and now < parse_time(item["start"]).timestamp()
            ):
                # No personal title/reference in unsolicited group notifications.
                key = row["key"] + ":remind:" + reminder["key"]
                if not c.execute(
                    select(db.outbox.c.id).where(db.outbox.c.dedupe_key == key)
                ).first():
                    enqueue(
                        c,
                        key,
                        "A requested trip reminder is due. "
                        "Reply what's next to view your saved next stop.",
                        now,
                        show_reference=False,
                    )
            reminder["sent"] = True
        schedule = data.get("checkin_time")
        day = local.date().isoformat()
        if schedule and local.strftime("%H:%M") >= schedule and data.get("checkin_sent") != day:
            key = "companion-checkin:" + day
            if not c.execute(select(db.outbox.c.id).where(db.outbox.c.dedupe_key == key)).first():
                enqueue(
                    c,
                    key,
                    "How was today's pace? Reply trip checkin tired / good / busy. "
                    "Optional; trip checkin off stops your daily prompt.",
                    now,
                    show_reference=False,
                )
            data["checkin_sent"] = day
        serialized = json.dumps(data)
        if serialized != row["value"]:
            c.execute(update(db.state).where(db.state.c.key == row["key"]).values(value=serialized))


def forget(c, sender):
    c.execute(delete(db.state).where(db.state.c.key == state_key(sender)))


def reply_context(snapshot):
    """A single compact footer only when an answer actually used travel tools."""
    evidence = snapshot.get("evidence", {})
    if not evidence:
        return ""
    failed = any(e.get("status") != "available" for e in evidence.values())
    available = any(e.get("status") == "available" for e in evidence.values())
    lines = (
        [
            "Some information could not be checked; available results are shown above."
            if available
            else "Live travel details could not be checked. Guidance is unverified."
        ]
        if failed
        else []
    )
    prefs = snapshot.get("family_preferences", [])
    labels = []
    for pref in prefs:
        # Only general food preferences in automatic footers, never personal limits.
        if pref["kind"] in {"vegetarian", "halal"} and json.loads(pref["value"]) is True:
            labels.append(pref["kind"])
    relevant = any(
        "openstreetmap" in e.get("source_url", "") or "comparison_id" in e.get("data", {})
        for e in evidence.values()
    )
    if labels and relevant:
        lines.append(
            "Preferences considered: "
            + ", ".join(sorted(set(labels)))
            + ". Suitability still depends on the evidence; say 'correct my preferences' to update."
        )
    return "\n".join(lines)
