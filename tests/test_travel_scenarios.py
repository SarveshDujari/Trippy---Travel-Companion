"""Thirty deterministic product scenarios; no claim of live model quality."""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from test_discussion import pending, private_reply, reply_for
from test_workflow import event, get, proposed

from trip_coordinator import discussion, features
from trip_coordinator import storage as db
from trip_coordinator.contracts import ToolRequest
from trip_coordinator.runtime import dispatch_once
from trip_coordinator.service import Coordinator
from trip_coordinator.travel_tools import execute

SCENARIOS = [
    "remember-walking",
    "correct-walking",
    "other-member-cannot-overwrite",
    "forget-preference",
    "forget-all",
    "memory-inspection",
    "hard-walking-blocks",
    "soft-walking-scores",
    "unknown-access-blocks",
    "fatigue-limit-blocks",
    "expense-split",
    "expense-paise",
    "expense-duplicate",
    "expense-invalid",
    "briefing-enable",
    "briefing-disable",
    "briefing-once",
    "calendar-export",
    "safe-links",
    "senior-toggle",
    "short-approval",
    "short-rejection",
    "child-cannot-approve",
    "expired-short-code",
    "delay-deduplication",
    "delay-invalidates-approval",
    "uncited-weather-blocked",
    "selective-tool-roundtrip",
    "walking-provider-failure",
    "uncertain-send-no-replay",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_family_scenario(setup, scenario):
    engine, config, now, service = setup

    def say(body, mid="feature", sender="parent"):
        service.consume(event(now[0], sender, body, mid))

    def remember():
        say("trip remember Dad max_walk_m 800 hard", "remember")

    if scenario in {
        "remember-walking",
        "correct-walking",
        "other-member-cannot-overwrite",
        "forget-preference",
        "forget-all",
        "memory-inspection",
    }:
        remember()
        if scenario == "correct-walking":
            say("trip remember Dad max_walk_m 1600 hard", "correct")
            assert json.loads(get(engine, db.preferences)[0]["value"]) == 1600
        elif scenario == "other-member-cannot-overwrite":
            say("trip remember Dad max_walk_m 5000 soft", "override", "child")
            assert json.loads(get(engine, db.preferences)[0]["value"]) == 800
        elif scenario in {"forget-preference", "forget-all"}:
            say(
                "trip forget preference Dad max_walk_m"
                if scenario == "forget-preference"
                else "trip forget",
                "forget",
            )
            assert not get(engine, db.preferences)
        elif scenario == "memory-inspection":
            say("trip memory", "inspect")
            assert '"evidence_id": "remember"' in get(engine, db.outbox)[-1]["body"]
        else:
            assert get(engine, db.preferences)[0]["author"] == "parent"
    elif scenario in {
        "hard-walking-blocks",
        "soft-walking-scores",
        "unknown-access-blocks",
        "fatigue-limit-blocks",
    }:
        kind, value = (
            ("wheelchair", "true")
            if scenario == "unknown-access-blocks"
            else (
                ("fatigue_limit", "10")
                if scenario == "fatigue-limit-blocks"
                else ("max_walk_m", "800")
            )
        )
        marker = "soft" if scenario == "soft-walking-scores" else "hard"
        say(f"trip remember Dad {kind} {value} {marker}")
        with engine.connect() as c:
            plans = features.demo_conflict(c)
        if scenario == "soft-walking-scores":
            assert all(p["feasible"] for p in plans)
            assert plans[0]["scores"]["Dad"] > plans[1]["scores"]["Dad"]
        elif scenario == "hard-walking-blocks":
            assert plans[0]["feasible"] and not plans[1]["feasible"]
        else:
            assert not any(p["feasible"] for p in plans)
    elif scenario.startswith("expense-"):
        amount = (
            "10.01"
            if scenario == "expense-paise"
            else "-1200"
            if scenario == "expense-invalid"
            else "1200"
        )
        say(f"trip spent {amount} dinner | Dad,Mom,Me")
        if scenario == "expense-duplicate":
            say(f"trip spent {amount} dinner | Dad,Mom,Me")
        rows = get(engine, db.expenses)
        if scenario == "expense-invalid":
            assert not rows
        else:
            assert len(rows) == 1
            shares = json.loads(rows[0]["split"])
            assert sum(shares.values()) == rows[0]["amount_minor"]
            assert max(shares.values()) - min(shares.values()) <= 1
    elif scenario in {"briefing-enable", "briefing-disable", "briefing-once"}:
        say("trip briefing 08:00")
        if scenario == "briefing-disable":
            say("trip briefing off", "off")
        with engine.connect() as c:
            schedule = c.execute(
                select(db.state.c.value).where(db.state.c.key == "morning_schedule")
            ).scalar()
        assert schedule == ("" if scenario == "briefing-disable" else "08:00")
        if scenario == "briefing-once":
            # Explicit afternoon timestamp in India so it is after the schedule.
            stamp = 1793941200
            first = asyncio.run(
                features.maybe_briefing(engine, config, {"status": "unavailable"}, stamp)
            )
            second = asyncio.run(
                features.maybe_briefing(engine, config, {"status": "unavailable"}, stamp)
            )
            assert first and not second
    elif scenario == "calendar-export":
        say("trip calendar")
        body = get(engine, db.outbox)[0]["attachment_text"]
        assert body.count("BEGIN:VEVENT") == 4
        assert "SIMULATED" in body and "DTSTART:20261106T140000Z" in body
        assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    elif scenario == "safe-links":
        links = features.handoffs("Hyderabad & family")
        assert "%26" in links["hotels"] and "no payment" in links["note"]
        assert not get(engine, db.actions)
    elif scenario == "senior-toggle":
        say("trip senior on")
        assert get(engine, db.preferences)[0]["value"] == "true"
    elif scenario in {
        "short-approval",
        "short-rejection",
        "child-cannot-approve",
        "expired-short-code",
        "delay-invalidates-approval",
    }:
        proposal = proposed(setup)
        if scenario == "expired-short-code":
            now[0] += config.approval_ttl_seconds + 1
        if scenario == "delay-invalidates-approval":
            with engine.begin() as c:
                features.trigger_delay(c, config, "test-delay", 120, now[0])
        say(
            ("reject " if scenario == "short-rejection" else "approve ")
            + proposal["approval_code"],
            "decision",
            "child" if scenario == "child-cannot-approve" else "parent",
        )
        state = get(engine, db.tasks)[0]["state"]
        assert state == (
            "executing"
            if scenario == "short-approval"
            else "cancelled"
            if scenario in {"short-rejection", "delay-invalidates-approval"}
            else "awaiting_approval"
        )
        assert not get(engine, db.actions)
    elif scenario == "delay-deduplication":
        with engine.begin() as c:
            assert features.trigger_delay(c, config, "test-delay", 120, now[0])
            assert not features.trigger_delay(c, config, "test-delay", 120, now[0])
        assert len(get(engine, db.tasks)) == 1
        assert get(engine, db.trips)[0]["version"] == 2
        assert "dinner, hotel, transfer" in get(engine, db.outbox)[0]["body"]
    elif scenario == "uncited-weather-blocked":
        cfg, current = pending(setup)
        Coordinator(engine, cfg, lambda: now[0]).consume(
            private_reply(now[0], reply_for(current, reply="It will be 60 C.", uses_weather=True))
        )
        assert get(engine, db.discussions)[0]["status"] == "waiting"
    elif scenario == "selective-tool-roundtrip":
        cfg, current = pending(setup, "Check walking time", weather_status="not_requested")
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
        assert get(engine, db.discussions)[0]["status"] == "tool_pending"
        assert len(get(engine, db.tools)) == 1
        assert not get(engine, db.actions)
    elif scenario == "walking-provider-failure":
        calls = []

        async def handler(request):
            calls.append(str(request.url))
            return httpx.Response(503)

        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await execute(
                    engine, client, "route", {"start": [78.47, 17.36], "end": [78.48, 17.37]}
                )
                assert result["status"] == "unavailable"
                assert not result["data"]
                # Failure cache prevents repetitive provider traffic.
                await execute(
                    engine, client, "route", {"start": [78.47, 17.36], "end": [78.48, 17.37]}
                )

        asyncio.run(check())
        assert len(calls) == 2 and all("routed-foot" in call for call in calls)
    elif scenario == "uncertain-send-no-replay":
        say("trip tips")
        calls = []

        class Client:
            async def post(self, *args):
                calls.append(args)
                raise httpx.ReadTimeout("Reply lost after possible delivery")

        asyncio.run(dispatch_once(engine, Client(), config.room_id))
        asyncio.run(dispatch_once(engine, Client(), config.room_id))
        assert len(calls) == 1
        assert get(engine, db.outbox)[0]["status"] == "unknown"
