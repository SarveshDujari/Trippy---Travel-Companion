"""Conversation ownership, reversible memory and operator visibility scenarios."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from test_discussion import pending
from test_workflow import get

from trip_coordinator import discussion, family
from trip_coordinator import storage as db
from trip_coordinator.contracts import PreferenceDraft
from trip_coordinator.operator_api import create_app
from trip_coordinator.travel_tools import provider_get


@pytest.mark.parametrize("case", ["undo", "other-person", "expired", "changed", "replay"])
def test_soft_memory_undo_scenarios(setup, case):
    engine, _, now, _ = setup
    with engine.begin() as c:
        family.propose_preference(
            c,
            PreferenceDraft(
                subject="me",
                kind="vegetarian",
                value=True,
                hard=False,
                evidence="I prefer vegetarian meals",
            ),
            {
                "sender": "parent",
                "request": "I prefer vegetarian meals",
                "message_id": "soft",
            },
            now[0],
        )
        assert get(engine, db.preferences) == []  # transaction has not committed yet
    assert len(get(engine, db.preferences)) == 1
    code = get(engine, db.confirmations)[-1]["code"]
    if case == "changed":
        with engine.begin() as c:
            c.execute(db.preferences.update().values(value="false"))
    actor = "child" if case == "other-person" else "parent"
    at = now[0] + 601 if case == "expired" else now[0]
    with engine.begin() as c:
        if case in {"other-person", "expired", "changed"}:
            with pytest.raises(ValueError):
                family.command(c, "undo " + code, actor, "undo", at)
        else:
            family.command(c, "undo " + code, actor, "undo", at)
            if case == "replay":
                with pytest.raises(ValueError):
                    family.command(c, "undo " + code, actor, "again", at)
    assert bool(get(engine, db.preferences)) == (case in {"other-person", "expired", "changed"})


def test_conversation_context_identifies_speakers(setup):
    engine, _, _, _ = setup
    _, current = pending(setup, "What about that option?")
    with engine.connect() as c:
        context = discussion.context(c, current)
    assert context["current_sender"] == "parent"
    assert context["conversation"][-1]["message_id"] == current["message_id"]
    assert context["conversation"][-1]["sender"] == "parent"


def test_dashboard_data_is_private_and_read_only(setup):
    engine, config, _, _ = setup
    token = "x" * 40
    with TestClient(create_app(engine, config, token)) as client:
        assert client.get("/dashboard").status_code == 200
        assert client.get("/dashboard/state").status_code == 401
        response = client.get("/dashboard/state", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert len(response.json()["agents"]) == 3
        assert client.post("/dashboard/state").status_code == 405
        assert client.get("/trace").status_code == 401


@pytest.mark.parametrize("statuses", [[503, 200], [429], [503, 503]])
def test_provider_retry_is_bounded_and_respects_rate_limits(statuses):
    calls = []

    async def run():
        def respond(request):
            calls.append(request)
            return httpx.Response(statuses[len(calls) - 1], json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await provider_get(client, "https://example.test")

    result = asyncio.run(run())
    assert result.status_code == statuses[-1]
    assert len(calls) == len(statuses)


def test_fatigue_exposes_missing_evidence():
    from trip_coordinator.features import rank_plans

    ranked = rank_plans(
        [
            {
                "name": "Short outing",
                "walking_m": 400,
                "heat_c": 32,
                "stairs": None,
                "travel_minutes": 8,
                "places": ["Charminar"],
                "vegetarian": None,
                "halal": None,
                "wheelchair": None,
                "onsite_walking_unknown": True,
            }
        ],
        [{"subject": "Dad", "kind": "max_walk_m", "value": "800", "hard": True, "strength": 3}],
    )[0]
    assert ranked["fatigue"]["score"] is None
    assert ranked["fatigue"]["missing_inputs"] == ["stairs"]
    assert ranked["fatigue"]["known_inputs"]["walking_m"] == 400
    assert ranked["fatigue"]["onsite_walking_unknown"]
    assert not ranked["feasible"]
