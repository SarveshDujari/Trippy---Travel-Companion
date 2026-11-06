"""End-to-end deterministic specialist handoffs and authority boundaries."""

import json
from datetime import datetime

import pytest
from sqlalchemy import insert, update
from test_discussion import pending, reply_for
from test_workflow import event, get

from trip_coordinator import discussion, specialists
from trip_coordinator import storage as db
from trip_coordinator.contracts import RuntimeConfig
from trip_coordinator.runtime import binding
from trip_coordinator.service import Coordinator


def configured(config):
    return RuntimeConfig.model_validate(
        {
            **config.model_dump(),
            "work_room_id": "private",
            "researcher_participant_id": "researcher",
            "mediator_participant_id": "mediator",
            "watcher_participant_id": "watcher",
        }
    )


def send(service, now, sender, payload, mid):
    envelope = event(now, sender, json.dumps(payload), mid)
    envelope.update(room_id="private", bridge_id=None)
    service.consume(envelope)


def dispatch(engine, config, current, now):
    with engine.begin() as c:
        discussion.dispatch(c, current, config, {"status": "not_requested"}, now)
    return get(engine, db.discussions)[-1]


def delegated(setup, role="researcher"):
    engine, _, now, _ = setup
    config, current = pending(setup, "Help us compare a comfortable outing")
    config = configured(config)
    service = Coordinator(engine, config, lambda: now[0])
    send(
        service,
        now[0],
        "planner",
        {
            "kind": "trip_delegate",
            "discussion_id": current["id"],
            "assignment_id": current["assignment_id"],
            "role": role,
            "objective": "Find relevant evidence",
        },
        "delegate",
    )
    return engine, config, now, service


def result(current, role, **kwargs):
    return {
        "kind": "trip_specialist_result",
        "discussion_id": current["id"],
        "assignment_id": current["assignment_id"],
        "role": role,
        "summary": "Evidence is incomplete.",
        **kwargs,
    }


def test_researcher_tool_and_return_to_planner(setup):
    engine, config, now, service = delegated(setup)
    current = get(engine, db.discussions)[-1]
    assert current["assigned_role"] == "researcher" and current["status"] == "queued"
    current = dispatch(engine, config, current, now[0])
    assert "@trip-researcher SPECIALIST_ASSIGNMENT" in get(engine, db.outbox)[-1]["body"]
    send(
        service,
        now[0],
        "researcher",
        {
            "kind": "trip_tool_request",
            "discussion_id": current["id"],
            "assignment_id": current["assignment_id"],
            "tool": "places",
            "arguments": {"query": "Charminar"},
        },
        "tool",
    )
    job = get(engine, db.tools)[-1]
    with engine.begin() as c:
        c.execute(
            update(db.tools)
            .where(db.tools.c.id == job["id"])
            .values(
                status="done",
                result=json.dumps(
                    {
                        "status": "available",
                        "source_url": "https://www.openstreetmap.org/",
                        "data": {"name": "Charminar"},
                    }
                ),
            )
        )
        c.execute(
            update(db.discussions)
            .where(db.discussions.c.id == current["id"])
            .values(status="queued")
        )
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    send(
        service,
        now[0],
        "researcher",
        result(
            current,
            "researcher",
            summary="Mapped place: Charminar.",
            claims=[{"source_id": job["id"], "field": "/name", "value": "Charminar"}],
        ),
        "research-result",
    )
    current = get(engine, db.discussions)[-1]
    assert current["assigned_role"] == "planner" and current["status"] == "queued"
    assert "Charminar" in json.loads(current["specialist_notes"])["researcher"]["verified_facts"]
    current = dispatch(engine, config, current, now[0])
    send(service, now[0], "planner", reply_for(current), "final")
    assert get(engine, db.discussions)[-1]["status"] == "completed"
    assert not get(engine, db.actions)


@pytest.mark.parametrize(
    "sender,room", [("mediator", "private"), ("researcher", "room"), ("planner", "private")]
)
def test_specialist_assignment_cannot_be_hijacked(setup, sender, room):
    engine, config, now, service = delegated(setup)
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    payload = result(current, "researcher") if sender != "planner" else reply_for(current)
    envelope = event(now[0], sender, json.dumps(payload), "hijack")
    envelope.update(room_id=room, bridge_id=None if room == "private" else "telegram")
    service.consume(envelope)
    assert get(engine, db.discussions)[-1]["status"] == "waiting"
    assert not get(engine, db.actions)


def test_researcher_cannot_delegate_or_mutate_family_memory(setup):
    engine, config, now, service = delegated(setup)
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    send(
        service,
        now[0],
        "researcher",
        reply_for(
            current,
            preferences=[
                {"subject": "me", "kind": "vegetarian", "value": True, "evidence": "fiction"}
            ],
        ),
        "mutate",
    )
    assert not get(engine, db.preferences)
    assert get(engine, db.discussions)[-1]["status"] == "waiting"


def test_specialist_cannot_be_called_twice_or_replay_old_result(setup):
    engine, config, now, service = delegated(setup)
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    payload = result(current, "researcher")
    send(service, now[0], "researcher", payload, "done")
    send(service, now[0], "researcher", payload, "replay")
    assert get(engine, db.discussions)[-1]["status"] == "queued"
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    send(
        service,
        now[0],
        "planner",
        {
            "kind": "trip_delegate",
            "discussion_id": current["id"],
            "assignment_id": current["assignment_id"],
            "role": "researcher",
            "objective": "Again",
        },
        "again",
    )
    assert get(engine, db.discussions)[-1]["assigned_role"] == "planner"
    assert get(engine, db.inbox)[-1]["outcome"] == "rejected"


@pytest.mark.parametrize("option,feasible", [(0, False), (1, True), (None, False)])
def test_mediator_respects_computed_feasibility(setup, option, feasible):
    engine, _, now, _ = setup
    config, current = pending(setup, "Help resolve our preferences")
    config = configured(config)
    service = Coordinator(engine, config, lambda: now[0])
    evidence = {
        "status": "available",
        "source_url": "https://example.test",
        "data": {
            "plans": [{"evaluation": {"feasible": False}}, {"evaluation": {"feasible": True}}]
        },
    }
    with engine.begin() as c:
        context = json.loads(current["context"])
        context["evidence"] = {"computed": evidence}
        c.execute(update(db.discussions).values(context=json.dumps(context)))
        c.execute(
            insert(db.tools).values(
                id="computed",
                discussion_id=current["id"],
                name="compare_plans",
                arguments="{}",
                status="done",
                result=json.dumps(evidence),
                created_at=now[0],
            )
        )
    send(
        service,
        now[0],
        "planner",
        {
            "kind": "trip_delegate",
            "discussion_id": current["id"],
            "assignment_id": current["assignment_id"],
            "role": "mediator",
            "objective": "Explain compromise",
        },
        "delegate",
    )
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    send(
        service,
        now[0],
        "mediator",
        result(current, "mediator", recommended_option=option),
        "mediate",
    )
    assert (get(engine, db.discussions)[-1]["assigned_role"] == "planner") == feasible
    assert not get(engine, db.actions)


def test_watcher_event_is_sourced_deduplicated_and_read_only(setup):
    engine, base, now, _ = setup
    config = configured(base)
    now[0] = int(datetime.fromisoformat("2026-11-06T12:00:00+05:30").timestamp())
    service = Coordinator(engine, config, lambda: now[0])
    with engine.begin() as c:
        assert specialists.weather_event(c, config, "2026-11-06T12:00:00+05:30", 61, now[0])
        assert not specialists.weather_event(c, config, "2026-11-06T12:00:00+05:30", 61, now[0])
        assert not specialists.weather_event(
            c, config, "2026-11-06T12:01:00+05:30", 63, now[0] + 60
        )
    current = dispatch(engine, config, get(engine, db.discussions)[-1], now[0])
    send(
        service,
        now[0],
        "watcher",
        result(
            current,
            "watcher",
            summary="Current conditions warrant review.",
            claims=[{"source_id": "weather_event", "field": "/weather_code", "value": 61}],
            affected_items=["transfer"],
            replan_suggested=True,
        ),
        "watch",
    )
    assert get(engine, db.discussions)[-1]["status"] == "completed"
    assert "Trip Watcher advisory" in get(engine, db.outbox)[-1]["body"]
    assert not get(engine, db.tasks) and not get(engine, db.actions)


@pytest.mark.parametrize(
    "at,source",
    [
        ("2026-11-06T23:00:00+05:30", "2026-11-06T23:00:00+05:30"),
        ("2026-11-06T12:00:00+05:30", "2026-11-05T12:00:00+05:30"),
    ],
)
def test_watcher_suppresses_quiet_hours_and_stale_events(setup, at, source):
    engine, config, _, _ = setup
    config = configured(config)
    now = int(datetime.fromisoformat(at).timestamp())
    with engine.begin() as c:
        assert not specialists.weather_event(c, config, source, 61, now)
    assert not get(engine, db.discussions)


def test_optional_roles_must_have_distinct_identities(setup):
    config = setup[1]
    with pytest.raises(ValueError):
        RuntimeConfig.model_validate(
            {
                **config.model_dump(),
                "work_room_id": "private",
                "researcher_participant_id": "planner",
            }
        )


def test_enabling_specialists_preserves_base_binding_when_idle(setup):
    engine, config, _, _ = setup
    config = config.model_copy(update={"work_room_id": "private"})
    binding(engine, config)
    binding(engine, configured(config))
    with pytest.raises(RuntimeError):
        binding(engine, configured(config).model_copy(update={"room_id": "different-family"}))
