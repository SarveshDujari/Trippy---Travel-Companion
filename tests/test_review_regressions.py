"""Regression coverage for all seven issues found in the previous review."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select, text
from test_discussion import pending, private_reply, reply_for
from test_workflow import event, get

from trip_coordinator import discussion
from trip_coordinator import storage as db
from trip_coordinator.runtime import dispatch_once, run
from trip_coordinator.service import Coordinator, seed
from trip_coordinator.switch import ProtocolStopped, SwitchClient


def test_forget_prevents_a_pending_context_from_being_sent(setup):
    engine, _, now, _ = setup
    config, _ = pending(setup, "Our budget is 45000 rupees")
    Coordinator(engine, config, lambda: now[0]).consume(
        event(now[0], "parent", "trip forget", "forget")
    )
    sent = []

    class Client:
        async def post(self, room_id, body):
            sent.append(body)

    asyncio.run(dispatch_once(engine, Client(), config.room_id))
    assert not any("45000" in body for body in sent)


def test_oversized_agent_envelope_does_not_crash_the_consumer(setup):
    _, _, now, service = setup
    value = event(now[0], "planner", "{" + "x" * 32000)
    crashed = False
    try:
        service.consume(value)
    except ValidationError:
        crashed = True
    assert not crashed


def test_shared_history_preserves_who_said_each_preference(setup):
    engine, _, now, service = setup
    config, current = pending(setup, "I need to avoid stairs")
    advisor = Coordinator(engine, config, lambda: now[0])
    advisor.consume(private_reply(now[0], reply_for(current)))
    service.consume(event(now[0], "child", "What are my walking preferences?", "child-query"))
    latest = get(engine, db.discussions)[-1]
    with engine.connect() as c:
        history = discussion.context(c, latest)["conversation"]
    humans = [message for message in history if message["role"] == "human"]
    assert len(humans) == 2
    assert all(message.get("sender") for message in humans)
    assert humans[0]["sender"] != humans[1]["sender"]


def test_weather_claim_not_in_evidence_is_not_published_as_checked(setup):
    engine, _, now, _ = setup
    config, current = pending(setup)
    # The stored context contains only an availability flag, no forecast numbers.
    fabricated = reply_for(
        current, reply="Tomorrow will definitely be 60 C with 100% rain.", uses_weather=True
    )
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], fabricated))
    assert not any("60 C" in item["body"] for item in get(engine, db.outbox))


def test_tomorrow_keeps_its_reference_time_when_queue_crosses_midnight(setup):
    engine, config, now, service = setup
    original = datetime(2026, 11, 5, 23, 59, 50, tzinfo=ZoneInfo("Asia/Kolkata"))
    now[0] = int(original.timestamp())
    service.consume(event(now[0], "parent", "How is the weather tomorrow?"))
    current = get(engine, db.discussions)[0]
    with engine.begin() as c:
        discussion.dispatch(
            c,
            current,
            config,
            {"status": "unavailable", "checked_at": "2026-11-06T00:01:00+05:30"},
            now[0] + 70,
        )
    snapshot = json.loads(get(engine, db.discussions)[0]["context"])
    assert snapshot.get("request_time") == original.isoformat()


def test_failed_heartbeat_interrupts_an_otherwise_idle_sse_stream():
    class IdleStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'event: connection_state\ndata: {"rooms":["room"],"cursor":0,'
                b'"heartbeat_interval_seconds":0.1}\n\n'
            )
            while True:
                await asyncio.sleep(0.02)
                yield b": keepalive\n\n"

    async def handler(request):
        if request.url.path.endswith("/connection/beat"):
            return httpx.Response(404)
        return httpx.Response(200, stream=IdleStream())

    async def check():
        client = SwitchClient(
            "http://localhost", "agent", "key", transport=httpx.MockTransport(handler)
        )
        heartbeat_failure_observed = False
        try:
            await asyncio.wait_for(client.listen("connection", "room", 0, None), timeout=0.35)
        except httpx.HTTPStatusError:
            heartbeat_failure_observed = True
        except TimeoutError:
            pass
        finally:
            await client.close()
        assert heartbeat_failure_observed

    asyncio.run(check())


def test_recovered_connections_do_not_exhaust_a_lifetime_budget(setup, monkeypatch):
    engine, config, now, _ = setup
    config = config.model_copy(update={"conversation_enabled": False})
    calls = []
    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)

    class Client:
        def __init__(self, *args):
            pass

        async def listen(self, connection_id, room_id, cursor, receive, on_state, **kwargs):
            calls.append(connection_id)
            await on_state({"cursor": 0})
            # Model an hour of healthy connection before a transient drop.
            now[0] += 3600
            if len(calls) == 6:
                raise ProtocolStopped("End review scenario after sixth healthy connection")
            raise httpx.ConnectError("transient Wi-Fi drop")

        async def close(self):
            pass

    monkeypatch.setattr("trip_coordinator.runtime.SwitchClient", Client)
    monkeypatch.setattr("trip_coordinator.runtime.asyncio.sleep", fast_sleep)
    monkeypatch.setattr("trip_coordinator.runtime.time.time", lambda: now[0])

    async def check():
        with pytest.raises(ExceptionGroup):
            await run(engine, config, "test-key")

    asyncio.run(check())
    assert len(calls) >= 6


def test_upgrade_from_original_schema_preserves_existing_trip_and_outbox(tmp_path):
    path = str(tmp_path / "legacy.db")
    config = Config("alembic.ini")
    config.set_main_option("trip_database_path", path)
    command.upgrade(config, "0001")
    engine = db.make_engine(path)
    try:
        seed(engine)
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO outbox(dedupe_key,body,status,attempts,next_attempt) "
                    "VALUES ('old','legacy message','pending',0,0)"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as c:
            assert c.execute(select(db.outbox.c.body)).scalar() == "legacy message"
            assert c.execute(select(db.outbox.c.show_reference)).scalar() is True
            assert c.execute(select(db.trips.c.id)).scalar() == "hyderabad-demo"
            assert c.execute(text("PRAGMA integrity_check")).scalar() == "ok"
            assert not c.execute(text("PRAGMA foreign_key_check")).all()
    finally:
        engine.dispose()


def test_model_reply_cannot_create_real_booking_actions(setup):
    engine, _, now, _ = setup
    config, current = pending(setup)
    payload = reply_for(current, reply="I've booked your real flight.")
    Coordinator(engine, config, lambda: now[0]).consume(private_reply(now[0], payload))
    # Conversation cannot grant execution authority; false booking prose is rejected too.
    assert not get(engine, db.actions)
    assert not get(engine, db.proposals)
    assert not get(engine, db.tasks)
