"""End-to-end utility commands plus weather, scheduling and edit boundaries."""

import json
import re
from datetime import datetime

import pytest
from sqlalchemy import select, update
from test_workflow import event, get

from trip_coordinator import companion, discussion, family
from trip_coordinator import storage as db
from trip_coordinator.contracts import DiscussionReply
from trip_coordinator.features import expense
from trip_coordinator.service import Coordinator
from trip_coordinator.verifier import Verifier


def at(value="2026-11-06T10:00:00+05:30"):
    return int(datetime.fromisoformat(value).timestamp())


def add(
    c,
    title="Park",
    kind="activity",
    start="2026-11-06T12:00:00+05:30",
    end="2026-11-06T13:00:00+05:30",
    sender="parent",
    location="Hyderabad",
):
    response = companion.command(
        c, f"trip add {title} | {kind} | {start} | {end} | {location}", sender, title, at()
    )
    code = re.search(r"yes (C[A-F0-9]+)", response)[1]
    family.decide(c, code, sender, True, at())
    return code + ":0"


def mark(c, identity, value):
    response = companion.command(c, f"trip mark {identity} {value}", "parent", value, at())
    code = re.search(r"yes (C[A-F0-9]+)", response)[1]
    family.decide(c, code, "parent", True, at())


def test_real_next_summary_and_allowance_do_not_use_demo(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        assert "No upcoming" in companion.next_card(c, "parent", at())
        identity = add(c)
        companion.command(c, f"trip travel {identity} 30", "parent", "travel", at())
        card = companion.next_card(c, "parent", at())
        assert "Park" in card and "11:30 IST" in card
        assert "maps/search" in card and "indoor" not in card
        assert "Park" not in companion.summary(c, "child", at())
        assert "flight" not in companion.summary(c, "parent", at()).lower()
        add(
            c,
            title="Hotel stay",
            kind="hotel",
            start="2026-11-05T15:00:00+05:30",
            end="2026-11-08T11:00:00+05:30",
        )
        assert "Next: Park" in companion.next_card(c, "parent", at())


@pytest.mark.parametrize(
    "case",
    [
        "clear",
        "rain",
        "heat",
        "stale",
        "demo",
        "wrong_city",
        "wrong_day",
        "unknown",
        "indoor",
        "early_heat",
    ],
)
def test_weather_alternatives_are_contextual(setup, case):
    engine, _, _, _ = setup
    with engine.begin() as c:
        identity = add(c)
        mark(c, identity, "outdoor" if case != "indoor" else "indoor")
        item = companion.stops(c, "parent")[0]
        weather = {
            "status": "available",
            "location": "Hyderabad",
            "fetched_at": at(),
            "checked_at": "2026-11-06T10:00:00+05:30",
            "daily": [
                {
                    "date": "2026-11-06",
                    "temperature_2m_max": 30,
                    "precipitation_probability_max": (
                        10 if case in {"clear", "heat", "early_heat"} else 70
                    ),
                }
            ],
        }
        if case == "rain":
            weather["daily"][0]["precipitation_probability_max"] = 70
        if case in {"heat", "early_heat"}:
            weather["daily"][0]["temperature_2m_max"] = 38
        if case == "early_heat":
            item["start"] = "2026-11-06T08:00:00+05:30"
        if case == "stale":
            weather["fetched_at"] -= 901
        if case == "demo":
            weather["demo_mode"] = True
        if case == "wrong_city":
            item["location"] = "Delhi"
        if case == "wrong_day":
            weather["daily"][0]["date"] = "2026-11-07"
        if case == "unknown":
            weather["status"] = "unavailable"
        result = companion.weather_hint(c, "parent", item, at(), weather)
        assert bool(result) == (case in {"rain", "heat"})


def test_indoor_model_advice_requires_trigger_or_request():
    reply = DiscussionReply(
        kind="trip_reply",
        discussion_id="d",
        assignment_id="a",
        reply="Consider an indoor alternative.",
    )
    with pytest.raises(ValueError, match="Indoor alternatives"):
        Verifier.validate(reply, {}, "Plan my day", {})
    Verifier.validate(reply, {"weather_alternative_triggers": ["rain"]}, "Plan my day", {})
    Verifier.validate(reply, {}, "Find indoor options", {})


@pytest.mark.parametrize("case", ["accept", "wrong_person", "expired", "stale", "decline"])
def test_edit_preview_boundaries(setup, case):
    engine, _, _, _ = setup
    with engine.begin() as c:
        identity = add(c)
        mark(c, identity, "flexible")
        response = companion.command(c, f"trip skip {identity}", "parent", "skip", at())
        code = re.search(r"yes (C[A-F0-9]+)", response)[1]
        assert companion.stops(c, "parent")
        if case == "stale":
            c.execute(
                update(db.imported).where(db.imported.c.id == identity).values(title="Changed")
            )
        if case in {"wrong_person", "expired", "stale"}:
            with pytest.raises(ValueError):
                family.decide(
                    c,
                    code,
                    "child" if case == "wrong_person" else "parent",
                    True,
                    at() + 601 if case == "expired" else at(),
                )
        else:
            family.decide(c, code, "parent", case == "accept", at())
            with pytest.raises(ValueError):
                family.decide(c, code, "parent", True, at())
        assert bool(companion.stops(c, "parent")) == (case != "accept")


def test_fixed_bookings_and_concurrent_overlap_are_protected(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        identity = add(c, kind="flight")
        with pytest.raises(ValueError, match="fixed"):
            companion.command(c, f"trip skip {identity}", "parent", "skip", at())
        with pytest.raises(ValueError, match="fixed"):
            mark(c, identity, "flexible")
        optional = add(
            c, title="Walk", start="2026-11-06T14:00:00+05:30", end="2026-11-06T15:00:00+05:30"
        )
        mark(c, optional, "flexible")
        response = companion.command(
            c, f"trip move {optional} 2026-11-06T16:00:00+05:30", "parent", "move", at()
        )
        code = re.search(r"yes (C[A-F0-9]+)", response)[1]
        add(
            c,
            title="Train",
            kind="train",
            start="2026-11-06T16:00:00+05:30",
            end="2026-11-06T17:00:00+05:30",
        )
        with pytest.raises(ValueError, match="overlaps"):
            family.decide(c, code, "parent", True, at())


def test_late_preview_keeps_bookings_and_does_not_mutate(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        identity = add(c)
        mark(c, identity, "flexible")
        add(
            c,
            title="Train",
            kind="train",
            start="2026-11-06T15:00:00+05:30",
            end="2026-11-06T16:00:00+05:30",
        )
        before = companion.stops(c, "parent")
        reply = companion.command(c, "we're running 20 minutes late", "parent", "late", at())
        assert "shortening or skipping Park" in reply and "Keep Train" in reply
        assert companion.stops(c, "parent") == before


def test_reminder_is_durable_deduplicated_and_not_delivered_late(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        add(c)
        companion.command(c, "remind me", "parent", "remind", at())
    due = at("2026-11-06T11:30:00+05:30")
    for _ in range(2):
        with engine.begin() as c:
            companion.tick(c, due)
    assert len(get(engine, db.outbox)) == 1
    with engine.begin() as c:
        companion.command(c, "remind me", "parent", "remind-again", at())
        companion.tick(c, at("2026-11-06T13:30:00+05:30"))
    assert len(get(engine, db.outbox)) == 1


def test_checkin_quiet_hours_opt_out_and_group_dedupe(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        for sender in ["parent", "child"]:
            companion.command(c, "trip checkin 20:30", sender, sender, at())
        companion.tick(c, at("2026-11-06T23:00:00+05:30"))
        assert not c.execute(select(db.outbox)).first()
        companion.tick(c, at("2026-11-07T20:30:00+05:30"))
        companion.tick(c, at("2026-11-07T20:31:00+05:30"))
        assert len(c.execute(select(db.outbox)).all()) == 1
        for sender in ["parent", "child"]:
            companion.command(c, "trip checkin off", sender, sender, at())
        companion.tick(c, at("2026-11-08T20:30:00+05:30"))
        assert len(c.execute(select(db.outbox)).all()) == 1


def test_budget_uses_own_shares_and_local_day(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        expense(c, "parent", "1200", "lunch", ["Me", "Dad"], "bill", at())
        expense(c, "child", "500", "snack", ["Me"], "other", at())
        expense(c, "parent", "100", "yesterday", ["Me"], "old", at("2026-11-05T23:59:00+05:30"))
        text = companion.command(c, "trip budget 1500", "parent", "budget", at())
        assert "Remaining: INR 900.00" in text
        assert "Set your daily" in companion.budget(c, "child", at())
        for amount in ["NaN", "Infinity", "-1", "0", "1.001"]:
            with pytest.raises(ValueError):
                companion.command(c, "trip budget " + amount, "parent", amount, at())


def test_checklist_and_feedback_are_personal_and_forgotten(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        companion.command(c, "trip done packing", "parent", "done", at())
        assert "Done: Packed" in companion.checklist(c, "parent", at())
        assert "To do: Packed" in companion.checklist(c, "child", at())
        companion.command(c, "today was tiring", "parent", "pace", at())
        assert companion.read(c, "parent")["feedback"]["pace"] == "tired"
        discussion.forget(c, "parent")
        assert companion.read(c, "parent") == {}


def test_gap_prompts_only_for_long_busy_day(setup):
    engine, _, _, _ = setup
    with engine.begin() as c:
        add(c, start="2026-11-06T10:00:00+05:30", end="2026-11-06T12:00:00+05:30")
        add(c, title="Walk", start="2026-11-06T12:00:00+05:30", end="2026-11-06T15:00:00+05:30")
        assert len(companion.gap_hints(companion.stops(c, "parent"))) == 2
        add(
            c,
            title="Lunch",
            kind="meal",
            start="2026-11-06T12:00:00+05:30",
            end="2026-11-06T12:30:00+05:30",
        )
        assert not any("meal" in x for x in companion.gap_hints(companion.stops(c, "parent")))
        assert companion.gap_hints([]) == []


def test_commands_route_without_model_and_budget_respects_privacy(setup):
    engine, config, now, service = setup
    now[0] = at()
    service.consume(event(now[0], "parent", "what's next?", "next"))
    assert "No upcoming" in get(engine, db.outbox)[-1]["body"]
    private = Coordinator(engine, config.model_copy(update={"group_privacy": True}), lambda: at())
    private.consume(event(at(), "parent", "trip budget 1500", "private-budget"))
    assert "not processed" in get(engine, db.outbox)[-1]["body"]
    with engine.connect() as c:
        assert not companion.read(c, "parent")


def test_footer_shows_only_general_preferences_and_partial_failure():
    result = companion.reply_context(
        {
            "evidence": {
                "places": {"status": "unavailable", "source_url": "https://openstreetmap.org"}
            },
            "family_preferences": [
                {"kind": "vegetarian", "value": json.dumps(True)},
                {"kind": "max_walk_m", "value": "800"},
            ],
        }
    )
    assert "could not be checked" in result and "vegetarian" in result
    assert "800" not in result
    assert companion.reply_context({}) == ""
