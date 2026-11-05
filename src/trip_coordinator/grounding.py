"""Validate declared factual claims against saved tool results."""

import re


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
    rendered, allowed = [], set(re.findall(r"\d+(?:\.\d+)?", request))
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
        allowed.update(re.findall(r"\d+(?:\.\d+)?", str(actual)))
        rendered.append(
            f"{claim.field.split('/')[-1].replace('_', ' ')}: {actual} "
            f"[source: {source['source_url']}]"
        )
    if reply.uses_weather and not any(
        c.source_id == "weather"
        or "open-meteo" in evidence.get(c.source_id, {}).get("source_url", "")
        for c in reply.claims
    ):
        raise ValueError("Weather facts need explicit field-level citations")
    # Limit factual numbers to evidence or the user's own input. Relative dates
    # and party/budget numbers in stored personal statements are also permitted.
    for fact in snapshot.get("this_persons_explicit_trip_details", []):
        allowed.update(re.findall(r"\d+(?:\.\d+)?", fact["value"]))
    for stamp in [snapshot.get("current_time", ""), snapshot.get("request_time", "")]:
        allowed.update(re.findall(r"\d+(?:\.\d+)?", stamp))
    if set(re.findall(r"\d+(?:\.\d+)?", reply.reply)) - allowed:
        raise ValueError("Numerical facts need evidence citations")
    if re.search(
        r"\b(?:I(?:'ve| have)|we have|successfully)\s+(?:booked|paid|reserved)\b", reply.reply, re.I
    ):
        raise ValueError("Conversation cannot claim booking execution")
    return "\n".join(rendered)
