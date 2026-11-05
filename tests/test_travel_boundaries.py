import asyncio
import json

import httpx
import pytest
from sqlalchemy import update
from test_discussion import pending, private_reply, reply_for
from test_workflow import get

from trip_coordinator import discussion
from trip_coordinator import storage as db
from trip_coordinator.contracts import ToolRequest
from trip_coordinator.features import trigger_delay
from trip_coordinator.operator_api import create_app
from trip_coordinator.runtime import dispatch_once
from trip_coordinator.service import Coordinator
from trip_coordinator.switch import SwitchClient
from trip_coordinator.travel_tools import execute


@pytest.mark.parametrize("allow_exceptions", [False, True])
def test_two_hour_disruption_requires_explicit_simulated_alternatives(setup, allow_exceptions):
    from test_workflow import event

    engine, config, now, service = setup
    with engine.begin() as c:
        trigger_delay(c, config, "two-hour-demo", 120, now[0])
    task = get(engine, db.tasks)[0]
    plan = {
        "kind": "trip_plan",
        "task_id": task["id"],
        "assignment_id": task["assignment_id"],
        "trip_version": 2,
        "summary": "SIMULATED late arrival repair",
        "changes": [
            {
                "item_id": "transfer",
                "start": "2026-11-06T22:15:00+05:30",
                "extra_cost_minor": 30000,
            },
            {
                "item_id": "hotel",
                "start": "2026-11-06T23:00:00+05:30",
                "extra_cost_minor": 0,
                "late_checkin": allow_exceptions,
            },
            {
                "item_id": "dinner",
                "start": "2026-11-06T23:45:00+05:30",
                "extra_cost_minor": 20000,
                "alternative": "room_service" if allow_exceptions else "keep",
            },
        ],
    }
    service.consume(event(now[0], "planner", json.dumps(plan), "late-plan"))
    if not allow_exceptions:
        assert not get(engine, db.proposals)
        return
    proposal = get(engine, db.proposals)[0]
    service.consume(
        event(now[0], "parent", "approve " + proposal["approval_code"], "late-approval")
    )
    task = get(engine, db.tasks)[0]
    payload = {
        "kind": "trip_execute",
        "task_id": task["id"],
        "assignment_id": task["assignment_id"],
        "proposal_id": proposal["id"],
    }
    execution = event(now[0], "reservation", json.dumps(payload), "late-execution")
    service.consume(execution)
    Coordinator(engine, config, lambda: now[0]).consume(execution)
    assert get(engine, db.tasks)[0]["state"] == "completed"
    assert len(get(engine, db.actions)) == 3
    assert all(x["status"] == "simulated_confirmed" for x in get(engine, db.actions))
    assert get(engine, db.trips)[0]["version"] == 3


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("places", {"query": 123}),
        ("places", {"query": "Hyderabad", "category": []}),
        ("weather", {"location": 123}),
        ("route", {"start": [True, 17], "end": [78, 17]}),
        ("route", {"start": [78, 17], "end": [78, 17], "mode": []}),
    ],
)
def test_bad_model_tool_arguments_do_not_crash_consumer(setup, tool, arguments):
    engine, _, now, _ = setup
    config, current = pending(setup)
    request = {
        "kind": "trip_tool_request",
        "discussion_id": current["id"],
        "assignment_id": current["assignment_id"],
        "tool": tool,
        "arguments": arguments,
    }
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], request))
    assert not get(engine, db.tools)
    assert get(engine, db.discussions)[0]["status"] == "waiting"


def test_operator_webhook_requires_token_and_deduplicates(setup):
    engine, config, _, _ = setup
    token = "test-token-" * 4

    async def check():
        transport = httpx.ASGITransport(app=create_app(engine, config, token))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            event = {"event_id": "flight-event-1", "minutes": 120}
            path = "/disruptions/flight-delay"
            assert (await client.post(path, json=event)).status_code == 401
            headers = {"Authorization": "Bearer " + token}
            assert (await client.post(path, json=event, headers=headers)).json()["processed"]
            assert not (await client.post(path, json=event, headers=headers)).json()["processed"]
            event["minutes"] = 90
            assert (await client.post(path, json=event, headers=headers)).status_code == 409
            assert (await client.post("/approve", headers=headers)).status_code == 404

    asyncio.run(check())
    assert len(get(engine, db.tasks)) == 1
    assert not get(engine, db.actions)


def test_route_tool_roundtrip_publishes_exact_citation(setup):
    engine, _, now, _ = setup
    config, current = pending(setup, "How far is the walk?", weather_status="not_requested")
    with engine.begin() as c:
        discussion.request_tool(
            c,
            ToolRequest(
                kind="trip_tool_request",
                discussion_id=current["id"],
                assignment_id=current["assignment_id"],
                tool="route",
                arguments={"start": [78.47, 17.36], "end": [78.48, 17.37]},
            ),
            now[0],
        )
    job = get(engine, db.tools)[0]

    async def check():
        async def handler(request):
            assert "/routed-foot/route/v1/foot/" in str(request.url)
            return httpx.Response(
                200, json={"code": "Ok", "routes": [{"distance": 650, "duration": 480}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execute(engine, client, "route", json.loads(job["arguments"]))

    result = asyncio.run(check())
    with engine.begin() as c:
        c.execute(
            update(db.tools)
            .where(db.tools.c.id == job["id"])
            .values(status="done", result=json.dumps(result))
        )
        c.execute(
            update(db.discussions)
            .where(db.discussions.c.id == current["id"])
            .values(status="queued")
        )
        next_turn = dict(current, status="queued")
        discussion.dispatch(c, next_turn, config, {"status": "not_requested"}, now[0])
    current = get(engine, db.discussions)[0]
    reply = reply_for(
        current,
        reply="The route estimate is 650 metres.",
        claims=[{"source_id": job["id"], "field": "/distance_m", "value": 650}],
    )
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], reply))
    assert get(engine, db.discussions)[0]["status"] == "completed"
    assert "https://routing.openstreetmap.de/routed-foot" in get(engine, db.outbox)[-1]["body"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("/current/temperature", 60),
        ("/missing", 29),
        ("/current", "29"),
        ("/current/temperature", True),
    ],
)
def test_false_or_malformed_claim_does_not_escape_validation(setup, field, value):
    engine, _, now, _ = setup
    config, current = pending(setup)
    reply = reply_for(
        current,
        reply="Checked weather.",
        uses_weather=True,
        claims=[{"source_id": "weather", "field": field, "value": value}],
    )
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], reply))
    assert get(engine, db.discussions)[0]["status"] == "waiting"
    assert not get(engine, db.chat)[-1]["role"] == "assistant"


def test_calendar_upload_uses_existing_switch_media_api(setup):
    engine, config, now, service = setup
    from test_workflow import event

    service.consume(event(now[0], "parent", "trip calendar"))
    calls = []

    async def check():
        async def handler(request):
            calls.append(request)
            assert request.url.path == "/agents/coordinator-id/rooms/room/media"
            assert b"BEGIN:VCALENDAR" in request.content
            assert b"text/calendar" in request.content
            return httpx.Response(200, json={"ok": True})

        client = SwitchClient(
            "http://localhost", "coordinator-id", "test-key", transport=httpx.MockTransport(handler)
        )
        try:
            await dispatch_once(engine, client, config.room_id)
        finally:
            await client.close()

    asyncio.run(check())
    assert len(calls) == 1
    assert get(engine, db.outbox)[0]["status"] == "sent"
