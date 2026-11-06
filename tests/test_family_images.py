import asyncio
import io

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from test_discussion import pending, private_reply, reply_for
from test_workflow import event, get

from trip_coordinator import discussion, family, media
from trip_coordinator import storage as db
from trip_coordinator.contracts import MemberDraft
from trip_coordinator.service import Coordinator
from trip_coordinator.switch import SwitchClient


def natural_preference(setup, subject="me"):
    engine, _, now, _ = setup
    text = "Keep walking at most 800 metres"
    config, current = pending(setup, text)
    service = Coordinator(engine, config, lambda: now[0])
    service.consume(
        private_reply(
            now[0],
            reply_for(
                current,
                preferences=[
                    {
                        "subject": subject,
                        "kind": "max_walk_m",
                        "value": 800,
                        "hard": True,
                        "evidence": text,
                        "operation": "set",
                    }
                ],
            ),
        )
    )
    return service, get(engine, db.confirmations)[-1]


def test_natural_preference_requires_exact_confirmation(setup):
    engine, _, now, _ = setup
    service, draft = natural_preference(setup)
    assert not get(engine, db.preferences)
    service.consume(event(now[0], "parent", "yes", "casual"))
    assert not get(engine, db.preferences)
    service.consume(event(now[0], "parent", "yes " + draft["code"], "confirm"))
    pref = get(engine, db.preferences)[0]
    assert pref["subject"] == family.member_id("parent")
    assert pref["value"] == "800"
    assert not get(engine, db.actions)


def test_natural_remove_does_not_need_to_repeat_the_old_numeric_limit(setup):
    engine, _, now, _ = setup
    service, draft = natural_preference(setup)
    service.consume(event(now[0], "parent", "yes " + draft["code"], "set"))
    from trip_coordinator.contracts import PreferenceDraft

    with engine.begin() as c:
        family.propose_preference(
            c,
            PreferenceDraft(
                subject="me",
                kind="max_walk_m",
                value=800,
                evidence="Forget my walking limit",
                operation="remove",
            ),
            {"sender": "parent", "request": "Forget my walking limit", "message_id": "remove"},
            now[0],
        )
    removal = get(engine, db.confirmations)[-1]
    service.consume(event(now[0], "parent", "yes " + removal["code"], "remove-confirm"))
    assert not get(engine, db.preferences)


def test_explicit_command_cannot_bypass_a_linked_relatives_consent(setup):
    engine, _, now, service = setup
    service.consume(
        event(
            now[0], "parent", "trip remember " + family.member_id("child") + " max_walk_m 800 hard"
        )
    )
    assert not get(engine, db.preferences)


def test_existing_reported_preferences_migrate_without_claiming_a_verified_identity(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    migration = Config("alembic.ini")
    path = str(tmp_path / "old.db")
    migration.set_main_option("trip_database_path", path)
    command.upgrade(migration, "0003")
    engine = db.make_engine(path)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO family_preferences(subject,kind,value,hard,strength,author,"
                    "evidence_id,updated_at) "
                    "VALUES ('Dad','max_walk_m','800',1,3,'parent','evidence',1)"
                )
            )
        command.upgrade(migration, "head")
        with engine.connect() as c:
            member = c.execute(select(db.family)).mappings().one()
            pref = c.execute(select(db.preferences)).mappings().one()
            assert member["participant_id"] is None and member["name"] == "Dad"
            assert pref["subject"] == member["id"] and pref["value"] == "800"
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", ["wrong-person", "expired", "duplicate"])
def test_confirmation_identity_expiry_and_replay(setup, failure):
    engine, _, now, _ = setup
    service, draft = natural_preference(setup)
    if failure == "expired":
        now[0] += 601
    sender = "child" if failure == "wrong-person" else "parent"
    value = event(now[0], sender, "yes " + draft["code"], "confirmation")
    service.consume(value)
    if failure == "duplicate":
        service.consume(event(now[0], sender, value["payload"]["body"], "second-confirmation"))
        assert len(get(engine, db.preferences)) == 1
    else:
        assert not get(engine, db.preferences)


def test_relative_preference_needs_that_linked_person(setup):
    engine, _, now, _ = setup
    service, draft = natural_preference(setup, family.member_id("child"))
    assert draft["confirmer"] == "child"
    service.consume(event(now[0], "parent", "yes " + draft["code"], "wrong"))
    assert not get(engine, db.preferences)
    service.consume(event(now[0], "child", "yes " + draft["code"], "right"))
    assert get(engine, db.preferences)[0]["author"] == "child"


def test_reported_companion_is_unlinked_then_merges_only_with_consent(setup):
    engine, _, now, _ = setup
    service, draft = natural_preference(setup, "Dad")
    service.consume(event(now[0], "parent", "yes " + draft["code"], "confirm"))
    with engine.begin() as c:
        dad = family.resolve(c, "parent", "Dad")
        assert dad["participant_id"] is None
        request = "Link Dad to the selected linked member"
        family.propose_member(
            c,
            MemberDraft(
                action="link_alias",
                name="Dad",
                member_id=family.member_id("child"),
                evidence=request,
            ),
            {"sender": "parent", "request": request, "message_id": "link"},
            now[0],
        )
    link = get(engine, db.confirmations)[-1]
    service.consume(event(now[0], "parent", "yes " + link["code"], "not-target"))
    assert get(engine, db.preferences)[0]["subject"] == dad["id"]
    service.consume(event(now[0], "child", "yes " + link["code"], "target-consent"))
    assert get(engine, db.preferences)[0]["subject"] == family.member_id("child")
    with engine.connect() as c:
        assert family.resolve(c, "parent", "Dad")["participant_id"] == "child"


def test_reported_nicknames_are_scoped_to_the_reporter(setup):
    engine, _, now, _ = setup
    with engine.begin() as c:
        first = family._reported_member(c, "parent", "Dad", now[0])
        second = family._reported_member(c, "child", "Dad", now[0])
        assert first != second
        assert family.resolve(c, "parent", "Dad")["id"] == first
        assert family.resolve(c, "child", "Dad")["id"] == second


def test_schema_repair_is_bounded_and_does_not_bypass_claim_validation(setup):
    engine, _, now, _ = setup
    config, current = pending(setup)
    service = Coordinator(engine, config, lambda: now[0])
    bad = reply_for(current, claims=[{"source_id": "weather", "field": "/current", "value": []}])
    service.consume(private_reply(now[0], bad))
    assert get(engine, db.discussions)[0]["status"] == "queued"
    assert not any(x["room_id"] is None for x in get(engine, db.outbox))
    with engine.begin() as c:
        discussion.dispatch(
            c, get(engine, db.discussions)[0], config, {"status": "not_requested"}, now[0]
        )
    refreshed = get(engine, db.discussions)[0]
    another = private_reply(now[0], reply_for(refreshed, claims=bad["claims"]))
    another["payload"]["message_id"] = "second-bad-schema"
    service.consume(another)
    assert get(engine, db.discussions)[0]["status"] == "waiting"
    assert get(engine, db.inbox)[-1]["outcome"] == "rejected"
    assert not get(engine, db.preferences)


def image_pending(setup):
    engine, config, now, service = setup
    service.consume(
        event(
            now[0],
            "parent",
            "What does this travel ticket say?",
            attachments=[{"mxc": "mxc://switch/image1", "mimetype": "image/png", "size": 1000}],
        )
    )
    current = get(engine, db.discussions)[0]
    config = config.model_copy(update={"work_room_id": "private"})
    with engine.begin() as c:
        discussion.dispatch(c, current, config, {"status": "not_requested"}, now[0])
    return config, get(engine, db.discussions)[0]


def test_image_import_is_a_reviewed_record_not_a_booking(setup):
    engine, _, now, _ = setup
    config, current = image_pending(setup)
    service = Coordinator(engine, config, lambda: now[0])
    service.consume(
        private_reply(
            now[0],
            reply_for(
                current,
                reply="The image shows a flight ticket.",
                image={
                    "summary": "Ticket image",
                    "extracted_text": "DEMO Flight to Hyderabad",
                    "itinerary": [
                        {"title": "Flight to Hyderabad", "kind": "flight", "location": "Hyderabad"}
                    ],
                },
            ),
        )
    )
    assert not get(engine, db.imported)
    proposal = get(engine, db.confirmations)[0]
    assert proposal["kind"] == "import"
    service.consume(event(now[0], "parent", "yes " + proposal["code"], "save-image"))
    saved = get(engine, db.imported)[0]
    assert saved["start"] is None
    assert "not_supplier_verified" in saved["status"]
    assert not get(engine, db.actions)


def test_image_caption_cannot_fabricate_a_personal_fact(setup):
    engine, _, now, _ = setup
    config, current = image_pending(setup)
    Coordinator(engine, config, lambda: now[0]).consume(
        private_reply(
            now[0],
            reply_for(
                current,
                facts=[
                    {
                        "key": "dates",
                        "value": "We travel on Friday",
                        "evidence": "We travel on Friday",
                    }
                ],
            ),
        )
    )
    assert not get(engine, db.facts)


@pytest.mark.parametrize(
    "reference",
    [
        {"mxc": "https://evil.example/image", "mimetype": "image/png"},
        {"mxc": "mxc://switch/valid", "mimetype": "application/pdf"},
        {"mxc": "mxc://switch/valid", "mimetype": "image/png", "size": 20_000_000},
    ],
)
def test_unsupported_or_untrusted_image_reference_is_rejected(reference):
    with pytest.raises(ValueError):
        media.validate_references([reference])


def test_corrupt_image_is_rejected():
    with pytest.raises(ValueError):
        media.checked_image(b"not actually a PNG")


def test_native_switch_media_reference_is_supported():
    media.validate_references(
        [{"mxc": "switch-media://4bdf127c1232444484c00ce63233b63b", "mimetype": "image/png"}]
    )


def test_images_forward_as_real_media_to_the_private_planner_room():
    content = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(content, format="PNG")
    calls = []

    async def check():
        async def handler(request):
            calls.append(request)
            if request.method == "GET":
                assert request.url.path.endswith("/family/media")
                return httpx.Response(200, content=content.getvalue())
            assert request.url.path.endswith("/private/media")
            assert b"DISCUSSION_ASSIGNMENT" in request.content
            assert b"image/jpeg" in request.content
            return httpx.Response(200, json={"ok": True})

        client = SwitchClient(
            "http://localhost", "coordinator", "test-key", transport=httpx.MockTransport(handler)
        )
        try:
            await client.upload_media(
                "private",
                {
                    "source_room": "family",
                    "forward": [{"mxc": "mxc://switch/image1", "mimetype": "image/png"}],
                },
                "@planner DISCUSSION_ASSIGNMENT",
            )
        finally:
            await client.close()

    asyncio.run(check())
    assert len(calls) == 2


def test_visual_is_generated_from_stored_state_only_when_requested(setup):
    engine, _, now, service = setup
    service.consume(event(now[0], "parent", "trip visual"))
    outgoing = get(engine, db.outbox)[0]
    assert "trippy-plan.png" in outgoing["media_json"]
    assert media.visual_requested("Show my itinerary as a timeline")
    assert not media.visual_requested("What should I pack?")
    assert not get(engine, db.actions)


def test_approval_card_retains_original_times_after_itinerary_updates(setup):
    from sqlalchemy import update
    from test_workflow import proposed

    from trip_coordinator.approvals import card

    engine, _, now, _ = setup
    proposal = proposed(setup)
    with engine.begin() as c:
        c.execute(
            update(db.items)
            .where(db.items.c.id == "hotel")
            .values(start="2026-11-06T22:00:00+05:30")
        )
        text = card(c, proposal, now[0])
        assert "Was: 06 Nov, 19:00 IST" in text
        assert "Proposed: 06 Nov, 21:00 IST" in text
        assert "TOTAL EXTRA: INR 500.00" in text


def test_own_display_name_changes_without_confirmation(setup):
    engine, _, now, _ = setup
    config, current = pending(setup, "Call me Sarvesh")
    service = Coordinator(engine, config, lambda: now[0])
    response = private_reply(
        now[0],
        reply_for(
            current,
            member_action={
                "action": "rename_self",
                "name": "Sarvesh",
                "evidence": "Call me Sarvesh",
                "member_id": family.member_id("child"),
            },
        ),
    )
    service.consume(response)
    members = {x["participant_id"]: x for x in get(engine, db.family)}
    assert members["parent"]["name"] == "Sarvesh"
    assert members["child"]["name"] != "Sarvesh"
    assert not get(engine, db.confirmations)
    service.consume(response)
    assert not get(engine, db.confirmations)


@pytest.mark.parametrize("action", ["family", "approvals", "status", "calendar", "propose_changes"])
def test_natural_assistant_handoff_never_executes_bookings(setup, action):
    engine, _, now, _ = setup
    config, current = pending(setup, "Please help with our trip")
    service = Coordinator(engine, config, lambda: now[0])
    service.consume(private_reply(now[0], reply_for(current, assistant_action=action)))
    assert not get(engine, db.actions)
    assert not get(engine, db.confirmations)
    if action == "propose_changes":
        assert get(engine, db.tasks)[0]["state"] == "planning"
    else:
        assert any(":assistant:" in x["dedupe_key"] for x in get(engine, db.outbox))
