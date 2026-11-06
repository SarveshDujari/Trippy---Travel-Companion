"""Deterministic checks for forecast scope and instruction-like provider content."""

import re
from datetime import date, datetime, timedelta

INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) instructions|"
    r"system prompt|approve\s+[A-Fa-f0-9]{6}|execute.{0,20}(?:payment|booking)|"
    r"(?:reveal|send|print).{0,25}(?:api.key|password|secret)",
    re.I,
)


def safe_data(value):
    if isinstance(value, str) and INJECTION.search(value):
        raise ValueError("Provider data contains instruction-like text")
    if isinstance(value, dict):
        for item in value.values():
            safe_data(item)
    if isinstance(value, list):
        for item in value:
            safe_data(item)


def requested_dates(request, snapshot):
    stamp = snapshot.get("request_time") or snapshot.get("current_time")
    if not stamp:
        return set()
    today = datetime.fromisoformat(stamp).date()
    text = request.casefold()
    explicit = set()
    for token in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        explicit.add(date.fromisoformat(token).isoformat())
    months = (
        "january february march april may june july august september october november december"
    ).split()
    for match in re.finditer(r"\b(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?\b", text):
        month = next((i for i, m in enumerate(months, 1) if match[2] in {m, m[:3]}), None)
        if month:
            explicit.add(date(int(match[3] or today.year), month, int(match[1])).isoformat())
    if explicit:
        # Explicit inclusive ranges, bounded to the forecast horizon.
        dates = sorted(explicit)
        if len(dates) == 2 and re.search(r"\b(?:to|through|until)\b| – | - ", text):
            first, last = map(date.fromisoformat, dates)
            if 0 <= (last - first).days <= 14:
                return {
                    (first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)
                }
        return explicit
    if "day after tomorrow" in text:
        return {(today + timedelta(days=2)).isoformat()}
    result = set()
    if "tomorrow" in text or "kal" in text.split():
        result.add((today + timedelta(days=1)).isoformat())
    if "today" in text or "aaj" in text.split() or "right now" in text:
        result.add(today.isoformat())
    weekdays = "monday tuesday wednesday thursday friday saturday sunday".split()
    for index, name in enumerate(weekdays):
        if re.search(r"\b" + name + r"\b", text):
            delta = (index - today.weekday()) % 7
            if "next " + name in text and delta == 0:
                delta = 7
            result.add((today + timedelta(days=delta)).isoformat())
    return result


def forecast_scope(reply, snapshot, request, evidence):
    dates = requested_dates(request, snapshot)
    if (
        not dates
        and reply.uses_weather
        and re.search(r"\b(?:next week|weekend|travel dates|trip dates)\b", request, re.I)
    ):
        raise ValueError("Please specify forecast dates for this request")
    for claim in reply.claims:
        source = evidence.get(claim.source_id, {})
        if claim.source_id != "weather" and "open-meteo" not in source.get("source_url", ""):
            continue
        data = source.get("data", {})
        parts = claim.field.strip("/").split("/")
        if parts[0] == "daily" and len(parts) >= 3:
            try:
                day = data["daily"][int(parts[1])]["date"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError("Invalid forecast field") from exc
            if dates and day not in dates:
                raise ValueError("Forecast citation is outside the requested date range")
        elif parts[0] == "current" and dates:
            current = str(data.get("current", {}).get("time", ""))[:10]
            if current not in dates:
                raise ValueError("Current conditions cannot answer a future-date question")


class Verifier:
    """One named entry point; model claims never constitute authorization."""

    @staticmethod
    def validate(reply, snapshot, request, evidence):
        for source in evidence.values():
            if source.get("status") == "available":
                safe_data(source.get("data", {}))
        forecast_scope(reply, snapshot, request, evidence)
        indoor_advice = re.search(
            r"\b(?:consider|suggest|recommend|try|choose|switch to|visit|offer)\b.{0,60}\bindoor\b",
            reply.reply + " " + " ".join(reply.suggestions),
            re.I,
        )
        if (
            indoor_advice
            and not snapshot.get("weather_alternative_triggers")
            and not re.search(r"\bindoor\b", request, re.I)
        ):
            raise ValueError(
                "Indoor alternatives need a relevant weather trigger or explicit request"
            )
        # Named places from available tools must be declared, not smuggled into prose.
        cited = {str(c.value).casefold() for c in reply.claims if isinstance(c.value, str)}

        def places(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"name", "display_name"} and isinstance(item, str):
                        short = item.split(",")[0].strip()
                        if len(short) >= 4 and short.casefold() in reply.reply.casefold():
                            if not any(short.casefold() in v for v in cited):
                                raise ValueError("Named tool places require an explicit claim")
                    places(item)
            elif isinstance(value, list):
                for item in value:
                    places(item)

        for source in evidence.values():
            places(source.get("data", {}))
