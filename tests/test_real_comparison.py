import asyncio
import json

import httpx
import pytest
from test_workflow import get

from trip_coordinator import storage as db
from trip_coordinator.features import set_preference
from trip_coordinator.travel_tools import execute


@pytest.mark.parametrize(
    "mode", ["success", "no-neighbour", "route-unavailable", "unknown-hard-access"]
)
def test_candidates_use_real_tool_results_and_fail_closed(setup, mode):
    engine, _, now, _ = setup
    if mode == "unknown-hard-access":
        with engine.begin() as c:
            set_preference(c, "Dad", "wheelchair", True, "parent", "access", hard=True, now=now[0])
    routes = []

    async def handler(request):
        if "nominatim" in str(request.url):
            origin = request.url.params.get("q") == "Hotel Hyderabad"
            return httpx.Response(
                200,
                json=[
                    {
                        "display_name": "Hotel Hyderabad" if origin else "Charminar Hyderabad",
                        "lat": "17.36" if origin else "17.3616",
                        "lon": "78.47" if origin else "78.4747",
                        "extratags": {},
                    }
                ],
            )
        if "overpass" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "elements": []
                    if mode == "no-neighbour"
                    else [
                        {
                            "type": "node",
                            "id": 123,
                            "lat": 17.3598,
                            "lon": 78.477,
                            "tags": {"name": "Mapped museum", "tourism": "museum"},
                        }
                    ]
                },
            )
        if "/route/" in str(request.url):
            routes.append(str(request.url))
            return (
                httpx.Response(503)
                if mode == "route-unavailable"
                else httpx.Response(
                    200, json={"code": "Ok", "routes": [{"distance": 350, "duration": 300}]}
                )
            )
        if "open-meteo" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "current": {"time": "2026-11-06T09:00", "temperature_2m": 29},
                    "daily": {
                        "time": ["2026-11-06"],
                        "weather_code": [1],
                        "temperature_2m_max": [30],
                        "temperature_2m_min": [22],
                        "precipitation_probability_max": [10],
                    },
                },
            )
        raise AssertionError("Unexpected provider")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execute(
                engine,
                client,
                "compare_plans",
                {
                    "origin": "Hotel Hyderabad",
                    "anchor": "Charminar Hyderabad",
                    "date": "2026-11-06",
                },
            )

    result = asyncio.run(run())
    if mode in {"no-neighbour", "route-unavailable"}:
        assert result["status"] == "unavailable"
        assert not get(engine, db.comparisons)
        return
    assert result["status"] == "available"
    plans = result["data"]["plans"]
    assert len(plans) == 2
    assert sorted(p["walking_m"] for p in plans) == [700, 1050]
    assert all(p["stairs"] is None and p["evaluation"]["fatigue"]["score"] is None for p in plans)
    assert all("routed-foot" in url for url in routes)
    assert len(get(engine, db.comparisons)) == 1
    assert not get(engine, db.actions)
    if mode == "unknown-hard-access":
        assert not any(p["evaluation"]["feasible"] for p in plans)
        assert all(p["evaluation"]["unknowns"] for p in plans)
    # Persisted proof includes the actual provider data, never model-invented metrics.
    assert json.loads(get(engine, db.comparisons)[0]["body"])["proof"]
