"""Small, bounded public-data clients. No straight-line/driving-as-walking fallback."""

import asyncio
import json
import math
import os
import time

import httpx
from sqlalchemy import select

from . import storage as db

_nominatim_lock = asyncio.Lock()
_last_geocode = 0.0


async def provider_get(client, url, **kwargs):
    """One retry for a transient provider failure; never retry rate limits."""
    for attempt in range(2):
        try:
            response = await client.get(url, **kwargs)
            if response.status_code not in {502, 503, 504} or attempt:
                return response
        except httpx.TransportError:
            if attempt:
                raise
        await asyncio.sleep(1.1)


def validate_request(name, arguments):
    if name not in {"weather", "places", "route", "compare_plans"} or not isinstance(
        arguments, dict
    ):
        raise ValueError("Unsupported tool request")
    if len(json.dumps(arguments)) > 1000:
        raise ValueError("Tool arguments too large")
    allowed = {
        "weather": {"location"},
        "places": {"query", "category", "enrich"},
        "route": {"start", "end", "mode"},
        "compare_plans": {"origin", "anchor", "date", "alternative"},
    }[name]
    if set(arguments) - allowed:
        raise ValueError("Unknown tool arguments")
    if name == "route":
        for key in ["start", "end"]:
            point = arguments.get(key)
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Route points must be [longitude, latitude]")
            if any(type(x) not in {int, float} or not math.isfinite(x) for x in point):
                raise ValueError("Invalid route coordinates")
            if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
                raise ValueError("Route coordinates out of range")
        if not isinstance(arguments.get("mode", "walking"), str) or arguments.get(
            "mode", "walking"
        ) not in {"walking", "driving"}:
            raise ValueError("Unsupported routing mode")
    if name == "places":
        if type(arguments.get("enrich", True)) is not bool:
            raise ValueError("Place enrichment must be true or false")
        if not isinstance(arguments.get("category", "attractions"), str) or arguments.get(
            "category", "attractions"
        ) not in {"attractions", "vegetarian", "halal", "family"}:
            raise ValueError("Unsupported place category")
        if not isinstance(arguments.get("query"), str) or not 1 <= len(arguments["query"]) <= 120:
            raise ValueError("A short place query is required")
    if name == "weather" and not isinstance(arguments.get("location", "Hyderabad"), str):
        raise ValueError("Weather location must be text")
    if name == "compare_plans":
        from datetime import date

        for key in ["origin", "anchor"]:
            if not isinstance(arguments.get(key), str) or not 1 <= len(arguments[key]) <= 120:
                raise ValueError("A real starting place and anchor attraction are required")
        if "alternative" in arguments and (
            not isinstance(arguments["alternative"], str)
            or not 1 <= len(arguments["alternative"]) <= 120
        ):
            raise ValueError("Alternative place query must be short text")
        try:
            date.fromisoformat(arguments.get("date", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("Comparison date must be YYYY-MM-DD") from exc
    return arguments


async def execute(engine, client, name, arguments, *, discussion_id=None):
    from .service import Coordinator
    from .weather import forecast

    validate_request(name, arguments)
    if name == "compare_plans":
        from .comparison import compare

        return await compare(engine, client, arguments, discussion_id=discussion_id)
    now = int(time.time())
    source = {
        "weather": "https://open-meteo.com/",
        "places": "https://www.openstreetmap.org/",
        "route": "https://routing.openstreetmap.de/",
    }[name]
    key = "toolcache:" + name + ":" + json.dumps(arguments, sort_keys=True)
    with engine.connect() as c:
        cached = c.execute(select(db.state.c.value).where(db.state.c.key == key)).scalar()
        demo = (
            c.execute(select(db.state.c.value).where(db.state.c.key == "cached-demo-mode")).scalar()
            == "true"
        )
    if demo:
        value = json.loads(cached) if cached else None
        if (
            value
            and value["status"] == "available"
            and 0 <= now - value["retrieved_at"] <= 7 * 86400
        ):
            return {
                **value,
                "demo_mode": True,
                "stale": True,
                "age_seconds": now - value["retrieved_at"],
                "label": "CACHED DEMO — historical evidence, not a live check",
            }
        return {
            "status": "unavailable",
            "demo_mode": True,
            "source_url": source,
            "retrieved_at": now,
            "data": {},
            "reason": "CACHED DEMO: no successful cached result within seven days",
        }
    if cached:
        value = json.loads(cached)
        ttl = (900 if name == "weather" else 86400) if value["status"] == "available" else 60
        if (
            name == "places"
            and value.get("data", {}).get("nearby_status") == "unavailable_or_empty"
        ):
            ttl = 60
        if 0 <= now - value["retrieved_at"] < ttl:
            value["cache"] = {
                "hit": True,
                "age_seconds": now - value["retrieved_at"],
                "expires_at": value["retrieved_at"] + ttl,
            }
            return value
    try:
        if name == "weather":
            if arguments.get("location", "Hyderabad").lower() not in {
                "hyderabad",
                "hyderabad, india",
                "hyderabad, telangana, india",
            }:
                raise ValueError("Live weather currently covers Hyderabad, India only")
            data = await forecast(engine, client)
            if data["status"] != "available":
                raise ValueError("Weather unavailable")
        elif name == "route":
            mode = arguments.get("mode", "walking")
            base = (
                os.getenv("TRIP_FOOT_ROUTER", "https://routing.openstreetmap.de/routed-foot")
                if mode == "walking"
                else "https://router.project-osrm.org"
            )
            if not base.startswith("https://"):
                raise ValueError("Routing endpoint must use HTTPS")
            points = ";".join(",".join(str(x) for x in arguments[k]) for k in ["start", "end"])
            response = await provider_get(
                client,
                base.rstrip("/")
                + "/route/v1/"
                + ("foot" if mode == "walking" else "driving")
                + "/"
                + points,
                params={"overview": "false", "alternatives": "false"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != "Ok" or not payload.get("routes"):
                raise ValueError("No supported route found")
            route = payload["routes"][0]
            if not all(
                isinstance(route.get(k), int | float) and math.isfinite(route[k]) and route[k] >= 0
                for k in ["distance", "duration"]
            ):
                raise ValueError("Invalid routing result")
            data = {
                "mode": mode,
                "distance_m": round(route["distance"]),
                "duration_minutes": round(route["duration"] / 60, 1),
                "start": arguments["start"],
                "end": arguments["end"],
            }
            source = base
        else:
            global _last_geocode
            async with _nominatim_lock:
                await asyncio.sleep(max(0, 1.1 - (time.monotonic() - _last_geocode)))
                response = await provider_get(
                    client,
                    os.getenv("TRIP_NOMINATIM", "https://nominatim.openstreetmap.org/search"),
                    params={
                        "q": arguments["query"],
                        "format": "jsonv2",
                        "limit": 3,
                        "extratags": 1,
                    },
                    headers={"User-Agent": "TrippyFamilyTravel/0.1 (personal prototype)"},
                )
                _last_geocode = time.monotonic()
            response.raise_for_status()
            matches = response.json()
            if not matches:
                raise ValueError("Place not found")
            place = matches[0]
            lat, lon = float(place["lat"]), float(place["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Geocoder returned invalid coordinates")
            tags = place.get("extratags", {})
            # Overpass is bounded and read-only; no user-supplied query language.
            category = arguments.get("category", "attractions")
            selector = {
                "attractions": '["tourism"~"attraction|museum"]',
                "vegetarian": '["amenity"="restaurant"]["diet:vegetarian"~"^(yes|only)$"]',
                "halal": '["amenity"="restaurant"]["diet:halal"~"^(yes|only)$"]',
                "family": "",
            }[category]
            if category == "family":
                query = (
                    f'[out:json][timeout:10];(nwr(around:1500,{lat},{lon})["tourism"~"attraction|museum"];'
                    f'nwr(around:1500,{lat},{lon})["amenity"="restaurant"];);out center tags 20;'
                )
            else:
                query = (
                    f"[out:json][timeout:10];nwr(around:1500,{lat},{lon})"
                    f"{selector};out center tags 12;"
                )
            nearby = []
            try:
                extra = (
                    await client.post(
                        os.getenv("TRIP_OVERPASS", "https://overpass-api.de/api/interpreter"),
                        data={"data": query},
                        headers={
                            "User-Agent": "TrippyFamilyTravel/0.2 (+https://github.com/SarveshDujari/Trippy---Travel-Companion)",
                            "Accept": "application/json",
                        },
                        timeout=8,
                    )
                    if arguments.get("enrich", True)
                    else None
                )
                if extra:
                    extra.raise_for_status()
                nearby = [
                    {
                        "name": e.get("tags", {}).get("name", "Unnamed place"),
                        "osm_url": f"https://www.openstreetmap.org/{e['type']}/{e['id']}",
                        "tags": {
                            k: str(v)[:200]
                            for k, v in e.get("tags", {}).items()
                            if k
                            in {
                                "name",
                                "tourism",
                                "amenity",
                                "diet:vegetarian",
                                "diet:halal",
                                "wheelchair",
                                "opening_hours",
                                "seats",
                            }
                        },
                        "latitude": e.get("lat", e.get("center", {}).get("lat")),
                        "longitude": e.get("lon", e.get("center", {}).get("lon")),
                    }
                    for e in (extra.json().get("elements", [])[:20] if extra else [])
                ]
            except Exception:
                # Main geocoding evidence remains useful; missing enrichment is explicit.
                pass
            data = {
                "name": place["display_name"],
                "latitude": lat,
                "longitude": lon,
                "opening_hours": tags.get("opening_hours", "unknown"),
                "wheelchair": tags.get("wheelchair", "unknown"),
                "seating": tags.get("seats", "unknown"),
                "nearby": nearby,
                "nearby_status": ("available" if nearby else "unavailable_or_empty")
                if arguments.get("enrich", True)
                else "not_requested",
                "note": (
                    "OSM tags are mapped claims, not a guarantee; "
                    "confirm important accessibility needs."
                ),
            }
        from .verifier import safe_data

        safe_data(data)
        result = {"status": "available", "source_url": source, "retrieved_at": now, "data": data}
        with engine.begin() as c:
            Coordinator._state(c, key, json.dumps(result))
        return result
    except Exception:
        # URLs/credentials and provider response bodies are not echoed to users.
        result = {
            "status": "unavailable",
            "source_url": source,
            "retrieved_at": now,
            "data": {},
            "reason": "Tool could not verify the requested information",
        }
        with engine.begin() as c:
            Coordinator._state(c, key, json.dumps(result))
        return result
