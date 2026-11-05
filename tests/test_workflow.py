import json

import pytest
from sqlalchemy import select, update

from trip_coordinator import storage as db
from trip_coordinator.service import Coordinator


def event(now, sender, body, mid="m1", **extra):
    return {
        "type": "message",
        "room_id": "room",
        "bridge_id": "telegram",
        "sequence": 1,
        "payload": {
            "sender": sender,
            "body": body,
            "message_id": mid,
            "timestamp": now * 1000,
            **extra,
        },
    }


def get(engine, table):
    with engine.connect() as c:
        return [dict(x) for x in c.execute(select(table)).mappings()]


def plan_payload(task):
    return {
        "kind": "trip_plan",
        "task_id": task["id"],
        "assignment_id": task["assignment_id"],
        "trip_version": task["version"],
        "summary": "Later transfer, check-in and dinner",
        "changes": [
            {
                "item_id": "transfer",
                "start": "2026-11-06T20:15:00+05:30",
                "extra_cost_minor": 30000,
            },
            {"item_id": "hotel", "start": "2026-11-06T21:00:00+05:30", "extra_cost_minor": 0},
            {"item_id": "dinner", "start": "2026-11-06T21:45:00+05:30", "extra_cost_minor": 20000},
        ],
    }


def proposed(setup):
    engine, _, now, service = setup
    service.consume(event(now[0], "parent", "trip plan fix delayed arrival"))
    payload = plan_payload(get(engine, db.tasks)[0])
    service.consume(event(now[0], "planner", json.dumps(payload), "plan-result"))
    return get(engine, db.proposals)[0]


def test_complete_protocol_workflow_retains_partial_success_after_restart(setup):
    engine, config, now, service = setup
    proposal = proposed(setup)
    service.consume(event(now[0], "parent", "trip approve " + proposal["id"], "approval"))
    task = get(engine, db.tasks)[0]
    payload = {
        "kind": "trip_execute",
        "task_id": task["id"],
        "assignment_id": task["assignment_id"],
        "proposal_id": proposal["id"],
    }
    restarted = Coordinator(engine, config, lambda: now[0])
    result = event(now[0], "reservation", json.dumps(payload), "execution")
    restarted.consume(result)
    restarted.consume(result)
    assert len(get(engine, db.actions)) == 3
    assert get(engine, db.tasks)[0]["state"] == "partially_completed"
    items = {x["id"]: x for x in get(engine, db.items)}
    assert "20:15" in items["transfer"]["start"]
    assert "20:30" in items["dinner"]["start"]
    assert get(engine, db.trips)[0]["version"] == 2
    assert get(engine, db.proposals)[0]["approved_by"] == "parent"


@pytest.mark.parametrize("sender,body", [("child", "approve"), ("parent", "yes")])
def test_scoped_human_approval_required(setup, sender, body):
    engine, _, now, service = setup
    proposal = proposed(setup)
    command = "trip approve " + proposal["id"] if body == "approve" else "yes"
    service.consume(event(now[0], sender, command, "unauthorized"))
    assert get(engine, db.tasks)[0]["state"] == "awaiting_approval"
    assert not get(engine, db.proposals)[0]["approved_by"]


@pytest.mark.parametrize("mutation", ["wrong_sender", "wrong_room", "wrong_bridge", "platform"])
def test_untrusted_identity_and_routing_cannot_approve(setup, mutation):
    engine, _, now, service = setup
    proposal = proposed(setup)
    value = event(now[0], "parent", "trip approve " + proposal["id"], "bad")
    if mutation == "wrong_sender":
        value["payload"]["sender"] = "unlinked"
        value["payload"]["sender_name"] = "parent"
    elif mutation == "platform":
        value["payload"]["sender_kind"] = "platform"
    else:
        value["room_id" if mutation == "wrong_room" else "bridge_id"] = "different"
    service.consume(value)
    assert get(engine, db.tasks)[0]["state"] == "awaiting_approval"


def test_duplicate_and_edited_message_does_not_create_second_task(setup):
    engine, _, now, service = setup
    value = event(now[0], "parent", "trip plan original")
    service.consume(value)
    value["payload"]["body"] = "trip plan edited"
    service.consume(value)
    assert len(get(engine, db.tasks)) == 1
    assert get(engine, db.tasks)[0]["request"] == "original"


@pytest.mark.parametrize("change", ["timing", "price", "duplicate", "flight", "naive", "version"])
def test_invalid_model_plan_does_not_become_proposal(setup, change):
    engine, _, now, service = setup
    service.consume(event(now[0], "parent", "trip plan repair"))
    payload = plan_payload(get(engine, db.tasks)[0])
    if change == "timing":
        payload["changes"][0]["start"] = "2026-11-06T18:00:00+05:30"
    elif change == "price":
        payload["changes"][0]["extra_cost_minor"] = 1
    elif change == "duplicate":
        payload["changes"].append(payload["changes"][0])
    elif change == "flight":
        payload["changes"][0]["item_id"] = "flight"
    elif change == "naive":
        payload["changes"][0]["start"] = "2026-11-06T20:15:00"
    else:
        payload["trip_version"] += 1
    service.consume(event(now[0], "planner", json.dumps(payload), "bad-plan"))
    assert not get(engine, db.proposals)
    assert get(engine, db.tasks)[0]["state"] == "planning"
    assert get(engine, db.inbox)[-1]["outcome"] == "rejected"


def test_stale_version_cannot_be_approved(setup):
    engine, _, now, service = setup
    proposal = proposed(setup)
    with engine.begin() as c:
        c.execute(update(db.trips).values(version=2))
    service.consume(event(now[0], "parent", "trip approve " + proposal["id"], "stale"))
    assert not get(engine, db.proposals)[0]["approved_by"]


def test_expiry_releases_trip_for_fresh_request(setup):
    engine, config, now, service = setup
    proposed(setup)
    now[0] += config.approval_ttl_seconds + 1
    service.expire()
    service.consume(event(now[0], "parent", "trip plan fresh", "fresh"))
    assert [x["state"] for x in get(engine, db.tasks)] == ["failed", "planning"]


def test_old_human_command_cannot_start_task(setup):
    engine, _, now, service = setup
    service.consume(event(now[0] - 121, "parent", "trip plan old"))
    assert not get(engine, db.tasks)


def test_execution_without_approval_rejected(setup):
    engine, _, now, service = setup
    proposal = proposed(setup)
    task = get(engine, db.tasks)[0]
    payload = {
        "kind": "trip_execute",
        "task_id": task["id"],
        "assignment_id": task["assignment_id"],
        "proposal_id": proposal["id"],
    }
    service.consume(event(now[0], "reservation", json.dumps(payload), "premature"))
    assert not get(engine, db.actions)


@pytest.mark.parametrize("change", ["expiry", "version", "quote"])
def test_execution_rechecks_approved_terms(setup, monkeypatch, change):
    from trip_coordinator.service import QUOTES

    engine, _, now, service = setup
    proposal = proposed(setup)
    service.consume(event(now[0], "parent", "trip approve " + proposal["id"], "approval"))
    task = get(engine, db.tasks)[0]
    if change == "expiry":
        with engine.begin() as c:
            c.execute(update(db.proposals).values(expires_at=now[0] - 1))
    elif change == "version":
        with engine.begin() as c:
            c.execute(update(db.trips).values(version=2))
    else:
        monkeypatch.setitem(QUOTES, "transfer", 40000)
    payload = {
        "kind": "trip_execute",
        "task_id": task["id"],
        "assignment_id": task["assignment_id"],
        "proposal_id": proposal["id"],
    }
    service.consume(event(now[0], "reservation", json.dumps(payload), "invalidated-execution"))
    assert not get(engine, db.actions)
