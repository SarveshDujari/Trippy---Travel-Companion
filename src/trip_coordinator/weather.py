"""Optional current-weather advisory. It never changes bookings or asserts trip risk."""

import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import insert, select

from . import storage as db
from .service import enqueue


def number(value):
    return value if isinstance(value, int | float) and math.isfinite(value) else None


async def forecast(engine, client, now=None):
    """Seven-day Hyderabad forecast; cached 15 minutes and explicitly timestamped."""
    from .service import Coordinator

    now = int(time.time()) if now is None else now
    checked = datetime.fromtimestamp(now, ZoneInfo("Asia/Kolkata")).isoformat()
    with engine.connect() as c:
        raw = c.execute(
            select(db.state.c.value).where(db.state.c.key == "forecast_hyderabad")
        ).scalar()
    cached = json.loads(raw) if raw else None
    if cached and 0 <= now - cached["fetched_at"] < 900:
        return {**cached, "cache_age_seconds": now - cached["fetched_at"]}
    try:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 17.385,
                "longitude": 78.4867,
                "timezone": "Asia/Kolkata",
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "forecast_days": 7,
            },
        )
        response.raise_for_status()
        payload = response.json()
        daily, current = payload["daily"], payload["current"]
        dates = daily["time"]
        fields = [
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
        ]
        if not dates or len(dates) > 7 or any(len(daily[k]) != len(dates) for k in fields):
            raise ValueError("Incomplete forecast")
        result = {
            "status": "available",
            "location": "Hyderabad, Telangana, India",
            "timezone": "Asia/Kolkata",
            "source": "https://open-meteo.com/",
            "checked_at": checked,
            "fetched_at": now,
            "temperature_unit": "C",
            "wind_unit": "km/h",
            "current": {
                "time": current["time"],
                "temperature": number(current.get("temperature_2m")),
                "weather_code": number(current.get("weather_code")),
                "wind_speed": number(current.get("wind_speed_10m")),
            },
            "daily": [
                {"date": day, **{k: number(daily[k][i]) for k in fields}}
                for i, day in enumerate(dates)
            ],
        }
        with engine.begin() as c:
            Coordinator._state(c, "forecast_hyderabad", json.dumps(result))
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {
            "status": "unavailable",
            "location": "Hyderabad, India",
            "checked_at": checked,
            "source": "https://open-meteo.com/",
            "instruction": "Live weather could not be checked. Do not invent conditions.",
        }


async def observe(engine, client: httpx.AsyncClient, now=None):
    now = int(time.time()) if now is None else now
    response = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": 17.385,
            "longitude": 78.4867,
            "current": "weather_code",
            "timezone": "Asia/Kolkata",
        },
    )
    response.raise_for_status()
    current = response.json()["current"]
    source_time, code = str(current["time"]), int(current["weather_code"])
    with engine.begin() as c:
        if c.execute(
            select(db.weather.c.id).where(
                db.weather.c.source_time == source_time,
                db.weather.c.code == code,
            )
        ).first():
            return
        last = c.execute(
            select(db.weather.c.observed_at)
            .where(db.weather.c.notified.is_(True))
            .order_by(db.weather.c.observed_at.desc())
        ).scalar()
        notify = code >= 51 and (last is None or now - last >= 3600)
        c.execute(
            insert(db.weather).values(
                source_time=source_time, code=code, observed_at=now, notified=notify
            )
        )
        if notify:
            enqueue(
                c,
                f"weather:{source_time}:{code}",
                f"Hyderabad current-weather advisory: WMO code {code} at {source_time} "
                "Asia/Kolkata. Source: Open-Meteo (https://open-meteo.com/). "
                "This is current weather, not a forecast for your travel dates. "
                "No itinerary changes have been made.",
                now,
            )
