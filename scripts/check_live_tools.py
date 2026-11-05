"""Read-only provider smoke checks using a disposable cache, no chat delivery."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from trip_coordinator import storage as db
from trip_coordinator.travel_tools import execute


async def main():
    with tempfile.TemporaryDirectory(prefix="trippy-tools-") as temp:
        engine = db.make_engine(str(Path(temp) / "checks.db"))
        db.metadata.create_all(engine)
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                weather = await execute(engine, client, "weather", {"location": "Hyderabad"})
                places = await execute(engine, client, "places", {"query": "Charminar Hyderabad"})
                # Routing smoke coordinates, not a family itinerary or proposed walking plan.
                route = await execute(
                    engine,
                    client,
                    "route",
                    {"start": [78.4747, 17.3616], "end": [78.4721, 17.3606], "mode": "walking"},
                )
            results = {
                name: {
                    "status": value["status"],
                    "source": value["source_url"],
                    "retrieved_at": value["retrieved_at"],
                    "data": value["data"],
                }
                for name, value in [("weather", weather), ("places", places), ("walking", route)]
            }
            target = Path("data/evaluation/live-tools.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(json.dumps({k: v["status"] for k, v in results.items()}))
        finally:
            engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
