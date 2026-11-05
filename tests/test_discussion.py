import asyncio
import hashlib
import json

import httpx
import pytest
from sqlalchemy import insert
from test_workflow import event, get

from trip_coordinator import discussion
from trip_coordinator import storage as db
from trip_coordinator.runtime import binding, dispatch_once
from trip_coordinator.service import Coordinator
from trip_coordinator.weather import forecast


def pending(setup, request="We are vegetarian", weather_status="available"):
    engine, config, now, service = setup
    service.consume(event(now[0], "parent", request))
    current = get(engine, db.discussions)[0]
    config = config.model_copy(update={"work_room_id": "private"})
    with engine.begin() as c:
        discussion.dispatch(
            c,
            current,
            config,
            {
                "status": weather_status,
                "checked_at": "2026-11-05T18:00:00+05:30",
                "current": {"temperature": 29},
            },
            now[0],
        )
    return config, get(engine, db.discussions)[0]


def reply_for(current, **extra):
    return {
        "kind": "trip_reply",
        "discussion_id": current["id"],
        "assignment_id": current["assignment_id"],
        "reply": "Let's plan a comfortable trip.",
        **extra,
    }


def private_reply(now, payload):
    result = event(now, "planner", json.dumps(payload), "reply")
    result.update(room_id="private", bridge_id=None)
    return result


def test_plain_language_queues_discussion_without_booking(setup):
    engine, _, now, service = setup
    service.consume(event(now[0], "parent", "Hey, can we discuss Hyderabad?"))
    assert len(get(engine, db.discussions)) == 1
    assert not get(engine, db.tasks)
    assert not get(engine, db.proposals)


def test_live_conversation_requires_private_workroom(setup):
    from trip_coordinator.runtime import run

    engine, config, _, _ = setup
    with pytest.raises(RuntimeError, match="private Switch workroom"):
        asyncio.run(run(engine, config, "test-key"))


def test_natural_sentence_starting_with_trip_is_still_conversation(setup):
    engine, _, now, service = setup
    service.consume(event(now[0], "parent", "trip should be relaxed, less walking please"))
    assert get(engine, db.discussions)[0]["request"].startswith("trip should")


def test_conversation_rate_limit_does_not_start_unbounded_agent_work(setup):
    engine, _, now, service = setup
    for index in range(9):
        service.consume(event(now[0], "parent", "Let's discuss the trip", str(index)))
    assert len(get(engine, db.discussions)) == 8
    assert "catching up" in get(engine, db.outbox)[-1]["body"]


def test_private_handoff_and_clean_family_reply(setup):
    engine, _, now, _ = setup
    config, current = pending(setup)
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], reply_for(current)))
    outgoing = get(engine, db.outbox)
    assert outgoing[0]["room_id"] == "private"
    assert outgoing[-1]["body"] == "Let's plan a comfortable trip."
    assert outgoing[-1]["room_id"] is None
    assert not outgoing[-1]["show_reference"]
    assert get(engine, db.discussions)[0]["status"] == "completed"


@pytest.mark.parametrize("sender", ["unlinked", "reservation"])
def test_only_planner_can_complete_discussion(setup, sender):
    engine, _, now, _ = setup
    config, current = pending(setup)
    value = private_reply(now[0], reply_for(current))
    value["payload"]["sender"] = sender
    Coordinator(engine, config, lambda: now[0]).consume(value)
    assert get(engine, db.discussions)[0]["status"] == "waiting"


def test_memory_preserves_full_statement_and_next_turn_context(setup):
    engine, _, now, _ = setup
    request = "I am not vegetarian; my mum is vegetarian"
    config, current = pending(setup, request)
    service = Coordinator(engine, config, lambda: now[0])
    service.consume(
        private_reply(
            now[0],
            reply_for(
                current,
                facts=[
                    {
                        "key": "food",
                        "value": request,
                        "evidence": request,
                    }
                ],
            ),
        )
    )
    service.consume(event(now[0], "parent", "What food should we try?", "next"))
    with engine.connect() as c:
        ctx = discussion.context(c, get(engine, db.discussions)[1])
    assert ctx["this_persons_explicit_trip_details"] == [{"key": "food", "value": request}]
    assert len(ctx["conversation"]) == 3
    assert not get(engine, db.actions)


def test_model_cannot_save_invented_or_negation_stripped_memory(setup):
    engine, _, now, _ = setup
    config, current = pending(setup, "We are not vegetarian")
    value = reply_for(
        current, facts=[{"key": "food", "value": "vegetarian", "evidence": "vegetarian"}]
    )
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], value))
    assert not get(engine, db.facts)
    assert get(engine, db.discussions)[0]["status"] == "waiting"


def test_weather_answer_requires_available_data_and_adds_source(setup):
    engine, _, now, _ = setup
    config, current = pending(setup)
    Coordinator(engine, config, lambda: now[0]).consume(
        private_reply(
            now[0],
            reply_for(
                current,
                uses_weather=True,
                claims=[{"source_id": "weather", "field": "/current/temperature", "value": 29}],
            ),
        )
    )
    assert "Open-Meteo" in get(engine, db.outbox)[-1]["body"]


def test_unavailable_weather_cannot_be_passed_off_as_checked(setup):
    engine, _, now, _ = setup
    config, current = pending(setup, weather_status="unavailable")
    Coordinator(engine, config, lambda: now[0]).consume(
        private_reply(now[0], reply_for(current, uses_weather=True))
    )
    assert get(engine, db.discussions)[0]["status"] == "waiting"


def test_forget_invalidates_old_reply_and_clears_local_context(setup):
    engine, _, now, _ = setup
    config, current = pending(setup)
    service = Coordinator(engine, config, lambda: now[0])
    service.consume(event(now[0], "parent", "trip forget", "forget"))
    service.consume(private_reply(now[0], reply_for(current)))
    assert not get(engine, db.discussions)
    assert not get(engine, db.chat)
    assert not get(engine, db.facts)
    assert get(engine, db.inbox)[-1]["outcome"] == "ignored_stale_discussion"


def test_duplicate_conversation_and_future_approval_not_executed(setup):
    engine, _, now, service = setup
    value = event(now[0], "parent", "Yes, book it")
    service.consume(value)
    service.consume(value)
    assert len(get(engine, db.discussions)) == 1
    assert not get(engine, db.actions)
    assert not get(engine, db.proposals)


def test_forecast_real_request_shape_cache_and_failure(setup):
    engine, _, now, _ = setup
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.url.params["forecast_days"] == "7"
        return httpx.Response(
            200,
            json={
                "current": {"time": "2026-11-05T18:00", "temperature_2m": 26},
                "daily": {
                    "time": ["2026-11-05", "2026-11-06"],
                    "weather_code": [0, 61],
                    "temperature_2m_max": [30, 29],
                    "temperature_2m_min": [20, 21],
                    "precipitation_probability_max": [0, 60],
                },
            },
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            one = await forecast(engine, client, now[0])
            two = await forecast(engine, client, now[0] + 1)
            assert one["daily"][1]["precipitation_probability_max"] == 60
            assert two["cache_age_seconds"] == 1

        async def offline(request):
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
            result = await forecast(engine, client, now[0] + 901)
            assert result["status"] == "unavailable" and "daily" not in result

    asyncio.run(check())
    assert len(calls) == 1


def test_legacy_binding_upgrade_preserves_original_identity_checks(setup):
    engine, config, _, _ = setup
    legacy = config.model_dump(
        exclude={
            "approval_ttl_seconds",
            "assignment_ttl_seconds",
            "work_room_id",
            "conversation_enabled",
        }
    )
    digest = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    with engine.begin() as c:
        c.execute(insert(db.state).values(key="binding", value=digest))
    binding(engine, config.model_copy(update={"work_room_id": "private"}))
    with pytest.raises(RuntimeError, match="config changed"):
        binding(
            engine, config.model_copy(update={"room_id": "attacker", "work_room_id": "private"})
        )


def test_outbox_routes_private_work_without_public_reference_leak(setup):
    engine, _, _, _ = setup
    pending(setup)
    sent = []

    class Client:
        async def post(self, room_id, body):
            sent.append((room_id, body))

    asyncio.run(dispatch_once(engine, Client(), "family"))
    assert sent[0][0] == "private" and "DISCUSSION_ASSIGNMENT" in sent[0][1]
