"""Two real, sourced outing candidates; models never invent routing metrics."""

import json
import math
import time
from uuid import uuid4

from sqlalchemy import insert, select

from . import storage as db
from .family import preferences
from .features import rank_plans


def _point(place):
    point = [place.get("longitude"), place.get("latitude")]
    if any(type(x) not in {int, float} or not math.isfinite(x) for x in point):
        raise ValueError("A candidate has no mapped coordinates")
    if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
        raise ValueError("Invalid mapped coordinates")
    return point


def _stop(place):
    return {
        "name": place["name"].split(",")[0][:120],
        "point": _point(place),
        "tags": place.get("tags", {}),
        "source_url": place.get("osm_url", "https://www.openstreetmap.org/"),
    }


def _diet(stops, key):
    meals = [s for s in stops if s["tags"].get("amenity") == "restaurant"]
    if not meals:
        return None
    values = [s["tags"].get(key) for s in meals]
    return True if all(x in {"yes", "only"} for x in values) else False if "no" in values else None


def _access(stops):
    values = [s["tags"].get("wheelchair") for s in stops]
    return True if all(x == "yes" for x in values) else False if "no" in values else None


async def compare(engine, client, arguments, *, discussion_id=None):
    from .travel_tools import execute

    now = int(time.time())
    unavailable = {
        "status": "unavailable",
        "source_url": "https://www.openstreetmap.org/",
        "retrieved_at": now,
        "data": {},
        "reason": (
            "Could not construct two sourced routes; please try a more precise origin/attraction"
        ),
    }
    proof = []

    async def checked(name, args):
        result = await execute(engine, client, name, args)
        proof.append({"tool": name, "arguments": args, **result})
        if result["status"] != "available":
            raise ValueError("Required mapping or routing evidence is unavailable")
        return result["data"]

    try:
        with engine.connect() as c:
            prefs = preferences(c)
            current = (
                c.execute(select(db.discussions).where(db.discussions.c.id == discussion_id))
                .mappings()
                .first()
                if discussion_id
                else None
            )
        origin = await checked("places", {"query": arguments["origin"], "enrich": False})
        anchor = await checked("places", {"query": arguments["anchor"], "category": "family"})
        start = _stop(origin)
        main = _stop(
            {
                **anchor,
                "tags": {
                    "wheelchair": anchor.get("wheelchair"),
                    "opening_hours": anchor.get("opening_hours"),
                },
            }
        )
        neighbours = []
        for place in anchor["nearby"]:
            try:
                candidate = _stop(place)
            except ValueError:
                continue
            if (
                candidate["name"].casefold() == main["name"].casefold()
                or candidate["point"] == main["point"]
            ):
                continue
            neighbours.append(candidate)
        attractions = [
            s for s in neighbours if s["tags"].get("tourism") in {"museum", "attraction"}
        ]
        if arguments.get("alternative") or not attractions:
            fallback = await checked(
                "places",
                {"query": arguments.get("alternative", "museum in Hyderabad"), "enrich": False},
            )
            candidate = _stop(fallback)
            dx = (candidate["point"][0] - main["point"][0]) * math.cos(
                math.radians(main["point"][1])
            )
            dy = candidate["point"][1] - main["point"][1]
            if candidate["point"] == main["point"] or math.hypot(dx, dy) * 111000 > 5000:
                return unavailable
            attractions = [candidate]
        attractions.sort(
            key=lambda s: sum((x - y) ** 2 for x, y in zip(s["point"], main["point"], strict=True))
        )
        extra = attractions[0]
        needs = [
            p["kind"]
            for p in prefs
            if p["kind"] in {"vegetarian", "halal"} and json.loads(p["value"]) is True
        ]
        restaurants = [
            s
            for s in neighbours
            if s["tags"].get("amenity") == "restaurant"
            and all(s["tags"].get("diet:" + kind) in {"yes", "only"} for kind in needs)
        ]
        meal = restaurants[:1] if needs else []
        weather = await execute(engine, client, "weather", {"location": "Hyderabad"})
        proof.append({"tool": "weather", **weather})
        day = next(
            (d for d in weather["data"].get("daily", []) if d["date"] == arguments["date"]), None
        )
        heat = day.get("temperature_2m_max") if day else None
        plans = []
        for name, destinations in [
            ("Shorter outing", [main, *meal]),
            ("Explore more", [main, extra, *meal]),
        ]:
            stops = [start, *destinations, start]
            legs = []
            for a, b in zip(stops, stops[1:], strict=False):
                leg = await checked(
                    "route", {"start": a["point"], "end": b["point"], "mode": "walking"}
                )
                legs.append(leg)
            plan = {
                "name": name,
                "walking_m": sum(x["distance_m"] for x in legs),
                "travel_minutes": round(sum(x["duration_minutes"] for x in legs), 1),
                "heat_c": heat,
                "stairs": None,
                "places": [s["name"] for s in destinations],
                "vegetarian": _diet(destinations, "diet:vegetarian"),
                "halal": _diet(destinations, "diet:halal"),
                "wheelchair": _access(destinations),
                "stops": stops,
                "legs": legs,
                "onsite_walking_unknown": True,
                "limitations": [
                    "Walking totals cover routed legs, not inside venues",
                    "Stairs and current opening status are unverified",
                    "OSM dietary/access tags need confirmation",
                ],
            }
            plan["evaluation"] = rank_plans([plan], prefs)[0]
            plans.append(plan)
        plans.sort(
            key=lambda p: (p["evaluation"]["feasible"], sum(p["evaluation"]["scores"].values())),
            reverse=True,
        )
        identity = "comparison_" + uuid4().hex[:12]
        data = {
            "comparison_id": identity,
            "date": arguments["date"],
            "origin": start["name"],
            "option_count": 2,
            "plans": plans,
            "proof": proof,
            "checked_at": now,
            "note": (
                "Proposed outings with mapped estimates; not bookings. "
                "Unknown hard requirements block feasibility."
            ),
        }
        with engine.begin() as c:
            if (
                discussion_id
                and not c.execute(
                    select(db.discussions.c.id).where(
                        db.discussions.c.id == discussion_id,
                        db.discussions.c.status == "tool_pending",
                    )
                ).first()
            ):
                return unavailable
            c.execute(
                insert(db.comparisons).values(
                    id=identity,
                    discussion_id=discussion_id,
                    sender=current["sender"] if current else "operator-demo",
                    body=json.dumps(data),
                    created_at=now,
                )
            )
        return {
            "status": "available",
            "source_url": "https://www.openstreetmap.org/",
            "retrieved_at": now,
            "data": data,
        }
    except (ValueError, KeyError, TypeError):
        return unavailable


def card(data):
    lines = [f"OUTING OPTIONS · {data['date']}", f"Starting point: {data['origin']}"]
    for index, plan in enumerate(data["plans"], 1):
        evaluation = plan["evaluation"]
        lines += [
            f"\n{index}. {plan['name']}",
            " → ".join(s["name"] for s in plan["stops"]),
            f"Mapped walking: {plan['walking_m']} m · route estimate: {plan['travel_minutes']} min",
        ]
        lines += [f"{name}: {score}/100" for name, score in evaluation["scores"].items()]
        if not evaluation["scores"]:
            lines.append("No confirmed family preferences yet; personal scores are not computed.")
        if evaluation["blockers"]:
            lines.append("Needs a change/check: " + "; ".join(evaluation["blockers"]))
        if evaluation["unknowns"]:
            lines.append("Unknown: " + "; ".join(evaluation["unknowns"]))
    lines += [
        "\nScores measure supplied preferences against known evidence; they do not assess safety.",
        "Venue walking, stairs and current opening status are not verified. No bookings changed.",
        "Sources: https://www.openstreetmap.org/ · https://routing.openstreetmap.de/",
        "Weather when available: https://open-meteo.com/",
    ]
    return "\n".join(lines)
