import asyncio
import json

import httpx
import pytest
from sqlalchemy import insert, select

from trip_coordinator import storage as db
from trip_coordinator.runtime import binding, dispatch_once, worker_lock
from trip_coordinator.switch import ProtocolStopped, SwitchClient, parse_sse
from trip_coordinator.weather import observe


async def lines(values):
    for value in values:
        yield value


def test_sse_comments_multiline_and_control_frames():
    async def check():
        result = [
            x
            async for x in parse_sse(
                lines(
                    [
                        ": keepalive",
                        "event: message",
                        "id: 12",
                        'data: {"type":"message",',
                        'data: "sequence":12}',
                        "",
                        "event: gap",
                        'data: {"reason":"expired"}',
                        "",
                    ]
                )
            )
        ]
        assert result[0].data["sequence"] == 12
        assert result[1].event == "gap"

    asyncio.run(check())


def test_protocol_transport_paths_and_replay_cursor():
    captured = []

    async def handler(request):
        captured.append(request)
        assert request.headers["Authorization"] == "Bearer private-test-key"
        if request.url.path.endswith("/events"):
            assert request.headers["Last-Event-ID"] == "41"
            assert request.url.params["rooms"] == "room"
            return httpx.Response(
                200,
                text=(
                    'event: connection_state\ndata: {"rooms":["room"],"cursor":41,'
                    '"heartbeat_interval_seconds":2}\n\n'
                    'event: message\nid: 42\ndata: {"sequence":42,"type":"message"}\n\n'
                ),
            )
        assert request.url.path == "/agents/coordinator/message"
        assert json.loads(request.content) == {"room_id": "room", "content": "test"}
        return httpx.Response(200, json={"ok": True})

    async def check():
        client = SwitchClient(
            "http://localhost:8000",
            "coordinator",
            "private-test-key",
            transport=httpx.MockTransport(handler),
        )
        seen = []

        async def receive(value):
            seen.append(value)

        await client.listen("stable-id", "room", 41, receive)
        await client.post("room", "test")
        await client.close()
        assert seen == [{"sequence": 42, "type": "message"}]
        assert len(captured) == 2

    asyncio.run(check())


def test_gap_stops_instead_of_skipping_history():
    async def handler(_):
        return httpx.Response(
            200,
            text=(
                'event: connection_state\ndata: {"rooms":["room"],"cursor":0,'
                '"heartbeat_interval_seconds":2}\n\nevent: gap\ndata: {}\n\n'
            ),
        )

    async def check():
        client = SwitchClient(
            "http://localhost", "agent", "key", transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(ProtocolStopped, match="gap"):
                await client.listen("connection", "room", 100, None)
        finally:
            await client.close()

    asyncio.run(check())


def test_failed_heartbeat_stops_before_a_later_gap():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'event: connection_state\ndata: {"rooms":["room"],"cursor":0,'
                b'"heartbeat_interval_seconds":0.1}\n\n'
            )
            await asyncio.sleep(0.15)
            yield b"event: gap\ndata: {}\n\n"

    async def handler(request):
        if request.url.path.endswith("/connection/beat"):
            return httpx.Response(404)
        return httpx.Response(200, stream=Stream())

    async def check():
        client = SwitchClient(
            "http://localhost", "agent", "key", transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.listen("connection", "room", 100, None)
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "mode,expected",
    [("ok", "sent"), ("timeout", "unknown"), ("connect", "pending"), ("rate", "pending")],
)
def test_delivery_outcomes_are_not_silently_retried(setup, mode, expected):
    engine, _, _, _ = setup
    with engine.begin() as c:
        c.execute(
            insert(db.outbox).values(
                dedupe_key="delivery", body="test", status="pending", attempts=0, next_attempt=0
            )
        )

    class Client:
        async def post(self, room_id, body):
            assert "ref:delivery" in body
            request = httpx.Request("POST", "http://localhost")
            if mode == "timeout":
                raise httpx.ReadTimeout("uncertain", request=request)
            if mode == "connect":
                raise httpx.ConnectError("offline", request=request)
            if mode == "rate":
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    asyncio.run(dispatch_once(engine, Client(), "room", now=100))
    with engine.connect() as c:
        assert c.execute(select(db.outbox.c.status)).scalar() == expected


def test_binding_persists_connection_id_and_rejects_room_change(setup):
    engine, config, _, _ = setup
    one = binding(engine, config)
    two = binding(engine, config)
    assert one["connection_id"] == two["connection_id"]
    with pytest.raises(RuntimeError, match="config changed"):
        binding(engine, config.model_copy(update={"room_id": "other"}))


def test_only_one_worker_can_own_database(setup):
    _, config, _, _ = setup
    with worker_lock(config.database_path), pytest.raises(RuntimeError, match="Another worker"):
        with worker_lock(config.database_path):
            pass


def test_weather_is_attributed_deduplicated_and_does_not_mutate_trip(setup):
    engine, _, _, _ = setup

    async def handler(_):
        return httpx.Response(
            200, json={"current": {"time": "2026-11-03T13:00", "weather_code": 61}}
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await observe(engine, client, now=10000)
            await observe(engine, client, now=10001)

    asyncio.run(check())
    with engine.connect() as c:
        assert len(c.execute(select(db.weather)).all()) == 1
        body = c.execute(select(db.outbox.c.body)).scalar()
        assert "Open-Meteo" in body and "not a forecast" in body
        assert not c.execute(select(db.tasks)).first()


def test_dispatch_waits_for_stream_readiness_and_checkpoints_initial_cursor(setup, monkeypatch):
    from trip_coordinator.runtime import run

    engine, config, _, _ = setup
    config = config.model_copy(update={"conversation_enabled": False})
    with engine.begin() as c:
        c.execute(
            insert(db.outbox).values(
                dedupe_key="startup", body="test", status="pending", attempts=0, next_attempt=0
            )
        )

    class Client:
        ready = False

        def __init__(self, *args):
            self.sent = asyncio.Event()

        async def listen(self, connection_id, room_id, cursor, receive, on_state, **kwargs):
            await asyncio.sleep(0.03)
            with engine.connect() as c:
                assert c.execute(select(db.outbox.c.status)).scalar() == "pending"
            self.ready = True
            await on_state({"cursor": 42})
            await self.sent.wait()
            raise ProtocolStopped("End controlled test stream")

        async def post(self, room_id, content):
            assert self.ready
            self.sent.set()

        async def close(self):
            pass

    monkeypatch.setattr("trip_coordinator.runtime.SwitchClient", Client)

    async def check():
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(run(engine, config, "test-key"), timeout=3)

    asyncio.run(check())
    with engine.connect() as c:
        assert (
            c.execute(select(db.state.c.value).where(db.state.c.key == "cursor")).scalar() == "42"
        )
        assert c.execute(select(db.outbox.c.status)).scalar() == "sent"


def test_inspect_works_while_runtime_owns_worker_lock(setup, tmp_path, monkeypatch, capsys):
    from trip_coordinator.__main__ import main

    _, config, _, _ = setup
    filename = tmp_path / "runtime.json"
    filename.write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["trip-coordinator", "inspect", "--config", str(filename)])
    with worker_lock(config.database_path):
        main()
    assert json.loads(capsys.readouterr().out) == {"tasks": [], "deliveries": []}
