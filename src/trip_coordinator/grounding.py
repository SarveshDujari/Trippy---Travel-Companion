"""Validate declared factual claims against saved tool results."""

import re
from decimal import Decimal


def numbers(value):
    """Compare numerical values, allowing ISO date padding and trailing zeroes."""
    return {
        Decimal(token.replace(",", ""))
        for token in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", str(value))
    }


def value_at(data, pointer):
    node = data
    if not pointer.startswith("/") or len(pointer) > 150:
        raise ValueError("Evidence field must be a JSON pointer")
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def verify(reply, snapshot, request):
    evidence = dict(snapshot.get("evidence", {}))
    if snapshot.get("weather", {}).get("status") == "available":
        evidence.setdefault(
            "weather",
            {
                "status": "available",
                "data": snapshot["weather"],
                "source_url": "https://open-meteo.com/",
            },
        )
    rendered, allowed = [], numbers(request)
    if reply.image is not None:
        if not snapshot.get("image_count"):
            raise ValueError("An image interpretation requires an attached image")
        allowed.update(numbers(reply.image.model_dump_json()))
    for claim in reply.claims:
        source = evidence.get(claim.source_id)
        if not source or source["status"] != "available":
            raise ValueError("Claim has no available evidence")
        try:
            actual = value_at(source["data"], claim.field)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("Claim field does not exist") from exc
        if actual is None or actual != claim.value or type(actual) is not type(claim.value):
            # Numeric JSON int/float are semantically compatible; booleans are not numbers.
            if not (
                type(actual) in {int, float}
                and type(claim.value) in {int, float}
                and actual == claim.value
            ):
                raise ValueError("Claim contradicts the tool result")
        if isinstance(actual, dict | list) or actual == "unknown":
            raise ValueError("Unknown or compound values cannot be verified claims")
        allowed.update(numbers(actual))
        parts = claim.field.split("/")
        field = parts[-2] if parts[-1].isdigit() and len(parts) > 2 else parts[-1]
        label = {
            "places": "Place",
            "walking_m": "Mapped walking (m)",
            "distance_m": "Route distance (m)",
            "travel_minutes": "Estimated route time (min)",
            "duration_minutes": "Estimated route time (min)",
        }.get(field, field.replace("_", " "))
        citation = f"{label}: {actual} [source: {source['source_url']}]"
        if citation not in rendered:
            rendered.append(citation)
    if reply.uses_weather and not any(
        c.source_id == "weather"
        or "open-meteo" in evidence.get(c.source_id, {}).get("source_url", "")
        for c in reply.claims
    ):
        raise ValueError("Weather facts need explicit field-level citations")
    # Limit factual numbers to evidence or the user's own input. Relative dates
    # and party/budget numbers in stored personal statements are also permitted.
    for fact in snapshot.get("this_persons_explicit_trip_details", []):
        allowed.update(numbers(fact["value"]))
    for stamp in [snapshot.get("current_time", ""), snapshot.get("request_time", "")]:
        allowed.update(numbers(stamp))
    if numbers(reply.reply) - allowed:
        raise ValueError("Numerical facts need evidence citations")
    if re.search(
        r"\b(?:I(?:'ve| have)|we have|successfully)\s+(?:booked|paid|reserved)\b", reply.reply, re.I
    ):
        raise ValueError("Conversation cannot claim booking execution")
    return "\n".join(rendered)
