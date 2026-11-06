"""Stable family identities and explicitly confirmed model-proposed changes."""

import hashlib
import json
import re
import secrets
from decimal import Decimal

from sqlalchemy import delete, insert, select, update

from . import storage as db


def member_id(identity):
    return "member_" + hashlib.sha256(identity.encode()).hexdigest()[:20]


def sync_members(c, config, now):
    for index, person in enumerate(config.members, 1):
        identity = member_id(person.participant_id)
        if not c.execute(select(db.family.c.id).where(db.family.c.id == identity)).first():
            c.execute(
                insert(db.family).values(
                    id=identity,
                    participant_id=person.participant_id,
                    name=f"Family member {index}",
                    created_by=person.participant_id,
                    created_at=now,
                )
            )


def directory(c):
    return [
        {
            "member_id": p["id"],
            "name": p["name"],
            "telegram_linked": p["participant_id"] is not None,
        }
        for p in c.execute(select(db.family)).mappings()
    ]


def resolve(c, sender, label):
    label = label.strip()
    if label.casefold() in {"me", "myself", "i"}:
        return (
            c.execute(select(db.family).where(db.family.c.participant_id == sender))
            .mappings()
            .first()
        )
    own = c.execute(
        select(db.aliases.c.member_id).where(
            db.aliases.c.owner == sender, db.aliases.c.alias == label.casefold()
        )
    ).scalar()
    if own:
        return c.execute(select(db.family).where(db.family.c.id == own)).mappings().one()
    matches = [
        p
        for p in c.execute(select(db.family)).mappings()
        if (p["id"] == label or p["name"].casefold() == label.casefold())
        and (p["participant_id"] is not None or p["created_by"] == sender)
    ]
    if len(matches) > 1:
        raise ValueError("That name is ambiguous; choose the member reference from trip family")
    return matches[0] if matches else None


def preferences(c):
    names = {p["id"]: p["name"] for p in c.execute(select(db.family)).mappings()}
    return [
        dict(
            p,
            member_id=p["subject"] if p["subject"] in names else None,
            subject=names.get(p["subject"], p["subject"]),
            identity_status="member record" if p["subject"] in names else "legacy reported label",
        )
        for p in c.execute(select(db.preferences)).mappings()
    ]


def _create(c, kind, sender, confirmer, message_id, payload, now):
    prefix = {"preference": "P", "member": "L", "import": "I", "expense": "E", "forget": "F"}[kind]
    code = prefix + secrets.token_hex(3).upper()
    while c.execute(select(db.confirmations.c.code).where(db.confirmations.c.code == code)).first():
        code = prefix + secrets.token_hex(3).upper()
    c.execute(
        insert(db.confirmations).values(
            code=code,
            kind=kind,
            sender=sender,
            confirmer=confirmer,
            message_id=message_id,
            payload=json.dumps(payload),
            status="pending",
            expires_at=now + 600,
            created_at=now,
        )
    )
    return code


def _evidence(quote, request):
    if quote not in request:
        raise ValueError("The proposed change must quote this message exactly")
    if "?" in quote or re.match(
        r"\s*(what if|suppose|for example|hypothetically)\b", request, re.I
    ):
        raise ValueError("Questions and hypothetical preferences cannot become saved constraints")


def propose_preference(c, draft, current, now):
    _evidence(draft.evidence, current["request"])
    value = draft.value
    if draft.kind in {"max_walk_m", "fatigue_limit"}:
        maximum = 20000 if draft.kind == "max_walk_m" else 100
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("Invalid preference limit")
        from .grounding import numbers

        numerals = numbers(draft.evidence)
        if (
            draft.operation == "set"
            and value not in numerals
            and not (
                draft.kind == "max_walk_m"
                and re.search(r"\bkm\b|kilomet", draft.evidence, re.I)
                and Decimal(value) / 1000 in numerals
            )
        ):
            raise ValueError("The numerical limit must appear in the quoted evidence")
    elif draft.kind == "required_place":
        if not isinstance(value, str) or not 1 <= len(value) <= 120:
            raise ValueError("Invalid place preference")
    elif type(value) is not bool:
        raise ValueError("This preference must be true or false")
    target = resolve(c, current["sender"], draft.subject)
    if target and target["participant_id"]:
        confirmer = target["participant_id"]
    else:
        confirmer = current["sender"]
    subject = target["id"] if target else None
    prior = (
        c.execute(
            select(db.preferences).where(
                db.preferences.c.subject == subject, db.preferences.c.kind == draft.kind
            )
        )
        .mappings()
        .first()
        if subject
        else None
    )
    if draft.operation == "remove" and (not prior or json.loads(prior["value"]) != value):
        raise ValueError("Choose the existing preference to remove; its saved value must match")
    payload = {
        "draft": draft.model_dump(),
        "member_id": subject,
        "expected": dict(prior) if prior else None,
    }
    # A newer correction supersedes older unanswered suggestions from this sender.
    for old in c.execute(
        select(db.confirmations).where(
            db.confirmations.c.kind == "preference",
            db.confirmations.c.sender == current["sender"],
            db.confirmations.c.status == "pending",
        )
    ).mappings():
        previous = json.loads(old["payload"])
        if (
            previous["draft"]["subject"] == draft.subject
            and previous["draft"]["kind"] == draft.kind
        ):
            c.execute(
                update(db.confirmations)
                .where(db.confirmations.c.code == old["code"])
                .values(status="superseded")
            )
    code = _create(
        c, "preference", current["sender"], confirmer, current["message_id"], payload, now
    )
    # Clear soft preferences owned by the sender are reversible without a ceremony.
    conflict = prior and (
        json.loads(prior["value"]) != value
        or prior["hard"] != draft.hard
        or draft.operation == "remove"
    )
    if target and target["participant_id"] == current["sender"] and not draft.hard and not conflict:
        decide(c, code, current["sender"], True, now)
        key = "undo-memory:" + code
        after = (
            c.execute(
                select(db.preferences).where(
                    db.preferences.c.subject == subject, db.preferences.c.kind == draft.kind
                )
            )
            .mappings()
            .first()
        )
        c.execute(
            insert(db.state).values(
                key=key,
                value=json.dumps(
                    {
                        "sender": current["sender"],
                        "subject": subject,
                        "kind": draft.kind,
                        "before": dict(prior) if prior else None,
                        "after": dict(after) if after else None,
                        "expires_at": now + 600,
                    }
                ),
            )
        )
        return (
            f"Updated your {draft.kind.replace('_', ' ')} preference. "
            f"Undo: undo {code} (10 minutes)."
        )
    name = target["name"] if target else draft.subject
    if not target or not target["participant_id"]:
        name += " (reported companion; not Telegram-linked)"
    consent = (
        "That linked member must confirm." if confirmer != current["sender"] else "Please confirm."
    )
    action = "Remove" if draft.operation == "remove" else "Save"
    labels = {
        "max_walk_m": "walking limit in metres",
        "fatigue_limit": "comfort-score limit",
        "vegetarian": "vegetarian meals",
        "halal": "halal meals",
        "required_place": "place to include",
        "wheelchair": "wheelchair access required",
        "senior": "senior comfort mode",
    }
    return (
        (
            f"Existing: {json.loads(prior['value'])} "
            f"({'hard limit' if prior['hard'] else 'preference'}), "
            f"evidence {prior['evidence_id']}\nProposed: "
            f"{'remove' if draft.operation == 'remove' else value}\n"
            f"New evidence: {draft.evidence}\nReplace only after confirmation.\n"
            if conflict
            else ""
        )
        + f"{action} for {name}: {labels[draft.kind]} = {value} "
        f"({'hard limit' if draft.hard else 'preference'})? {consent}\n"
        f"Reply: yes {code}\nOr: no {code}\nExpires in 10 minutes."
    )


def _link_source(c, sender, draft):
    source = resolve(c, sender, draft.name) if draft.action == "link_alias" else None
    if not source or source["participant_id"] or source["created_by"] != sender:
        return None
    prefs = [
        dict(p)
        for p in c.execute(
            select(db.preferences).where(db.preferences.c.subject == source["id"])
        ).mappings()
    ]
    return {"id": source["id"], "preferences": prefs}


def propose_member(c, draft, current, now):
    _evidence(draft.evidence, current["request"])
    if draft.action == "rename_self":
        target = resolve(c, current["sender"], "me")
        if not target:
            raise ValueError("This account has not been enrolled in the family directory")
        c.execute(
            update(db.family)
            .where(db.family.c.id == target["id"], db.family.c.participant_id == current["sender"])
            .values(name=draft.name)
        )
        return f"Got it — I'll call you {draft.name}."
    else:
        target = resolve(c, current["sender"], draft.member_id or "")
        if not target or not target["participant_id"]:
            raise ValueError(
                "Choose an already linked member from trip family; a name is not identity"
            )
    code = _create(
        c,
        "member",
        current["sender"],
        target["participant_id"],
        current["message_id"],
        {
            "draft": draft.model_dump(),
            "target": target["id"],
            "source": _link_source(c, current["sender"], draft),
        },
        now,
    )
    source = _link_source(c, current["sender"], draft)
    carried = (
        "\nReported needs included in this link: "
        + "; ".join(f"{p['kind']}={p['value']}" for p in source["preferences"])
        if source
        else ""
    )
    return (
        f"Confirm {'your display name' if draft.action == 'rename_self' else 'the alias'} "
        f"'{draft.name}' for {target['name']} ({target['id']}).\n"
        f"Only that linked account can confirm: yes {code}\nOr: no {code}\nExpires in 10 minutes."
        + carried
    )


def propose_import(c, reading, current, now):
    if not reading.itinerary:
        return None
    if not json.loads(current.get("attachments_json") or "[]"):
        raise ValueError("Image extraction requires an image attached to this request")
    payload = {"items": [x.model_dump(mode="json") for x in reading.itinerary]}
    code = _create(
        c, "import", current["sender"], current["sender"], current["message_id"], payload, now
    )
    lines = ["Read from your image — unconfirmed; please check:"]
    lines += [
        f"• {x.title}: {x.start.isoformat() if x.start else 'time unknown'}"
        + (f" → {x.end.isoformat()}" if x.end else "")
        + (f"; {x.location}" if x.location else "")
        for x in reading.itinerary
    ]
    lines += [
        "This saves trip information only, and does not book or modify anything.",
        f"Save: yes {code}",
        f"Discard: no {code}",
        "For corrections, send the corrected details before saving. Expires in 10 minutes.",
    ]
    return "\n".join(lines)


def _reported_member(c, sender, name, now):
    identity = member_id("reported:" + sender + ":" + name.casefold())
    if not c.execute(select(db.family.c.id).where(db.family.c.id == identity)).first():
        c.execute(
            insert(db.family).values(id=identity, name=name, created_by=sender, created_at=now)
        )
        c.execute(
            insert(db.aliases).values(owner=sender, alias=name.casefold(), member_id=identity)
        )
    return identity


def decide(c, code, sender, accept, now):
    from .features import set_preference

    entry = (
        c.execute(select(db.confirmations).where(db.confirmations.c.code == code.upper()))
        .mappings()
        .first()
    )
    if not entry or entry["status"] != "pending" or entry["expires_at"] <= now:
        raise ValueError("That confirmation is no longer pending; request a fresh suggestion")
    if entry["confirmer"] != sender:
        raise ValueError("Only the intended linked person can confirm this change")
    payload = json.loads(entry["payload"])
    if accept and entry["kind"] == "preference":
        draft = payload["draft"]
        identity = payload["member_id"] or _reported_member(
            c, entry["sender"], draft["subject"], now
        )
        prior = (
            c.execute(
                select(db.preferences).where(
                    db.preferences.c.subject == identity, db.preferences.c.kind == draft["kind"]
                )
            )
            .mappings()
            .first()
        )
        if (dict(prior) if prior else None) != payload["expected"]:
            raise ValueError(
                "This preference changed since the suggestion; request a fresh confirmation"
            )
        if draft["operation"] == "remove":
            c.execute(
                insert(db.events).values(
                    id="pref_" + secrets.token_hex(8),
                    kind="preference_changed",
                    payload=json.dumps(
                        {
                            "subject": identity,
                            "kind": draft["kind"],
                            "before": dict(prior),
                            "after": {
                                "author": sender,
                                "removed": True,
                                "evidence_id": entry["message_id"],
                            },
                        }
                    ),
                    status="recorded",
                    created_at=now,
                )
            )
            c.execute(
                delete(db.preferences).where(
                    db.preferences.c.subject == identity, db.preferences.c.kind == draft["kind"]
                )
            )
        else:
            set_preference(
                c,
                identity,
                draft["kind"],
                draft["value"],
                sender,
                entry["message_id"],
                hard=draft["hard"],
                strength=draft["strength"],
                now=now,
            )
    elif accept and entry["kind"] == "member":
        draft, target = payload["draft"], payload["target"]
        if draft["action"] == "rename_self":
            c.execute(
                update(db.family)
                .where(db.family.c.id == target, db.family.c.participant_id == sender)
                .values(name=draft["name"])
            )
        else:
            source = payload.get("source")
            if source:
                existing = [
                    dict(p)
                    for p in c.execute(
                        select(db.preferences).where(db.preferences.c.subject == source["id"])
                    ).mappings()
                ]
                if existing != source["preferences"]:
                    raise ValueError("Reported needs changed; request a fresh identity link")
                target_kinds = set(
                    c.execute(
                        select(db.preferences.c.kind).where(db.preferences.c.subject == target)
                    ).scalars()
                )
                if target_kinds & {p["kind"] for p in existing}:
                    raise ValueError(
                        "Resolve the two preference records before linking this member"
                    )
                c.execute(
                    update(db.preferences)
                    .where(db.preferences.c.subject == source["id"])
                    .values(subject=target, author=sender)
                )
                c.execute(
                    update(db.aliases)
                    .where(db.aliases.c.member_id == source["id"])
                    .values(member_id=target)
                )
                c.execute(
                    delete(db.family).where(
                        db.family.c.id == source["id"], db.family.c.participant_id.is_(None)
                    )
                )
            prior = (
                c.execute(
                    select(db.aliases).where(
                        db.aliases.c.owner == entry["sender"],
                        db.aliases.c.alias == draft["name"].casefold(),
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                c.execute(
                    update(db.aliases)
                    .where(
                        db.aliases.c.owner == entry["sender"],
                        db.aliases.c.alias == draft["name"].casefold(),
                    )
                    .values(member_id=target)
                )
            else:
                c.execute(
                    insert(db.aliases).values(
                        owner=entry["sender"], alias=draft["name"].casefold(), member_id=target
                    )
                )
    elif accept and entry["kind"] == "expense":
        from .features import expense

        expense(
            c,
            sender,
            payload["amount"],
            payload["description"],
            payload["participants"],
            entry["message_id"],
            now,
        )
    elif accept and entry["kind"] == "forget":
        from .discussion import forget

        forget(c, sender)
    elif accept and entry["kind"] == "import":
        for index, item in enumerate(payload["items"]):
            c.execute(
                insert(db.imported).values(
                    id=entry["code"] + f":{index}",
                    confirmation_code=entry["code"],
                    reported_by=sender,
                    source_message_id=entry["message_id"],
                    status="user_confirmed_information_not_supplier_verified",
                    **item,
                )
            )
    c.execute(
        update(db.confirmations)
        .where(db.confirmations.c.code == entry["code"])
        .values(status="confirmed" if accept else "declined", decided_at=now)
    )
    return (
        "Saved the confirmed trip information."
        if accept
        else "Discarded the suggestion. Nothing changed."
    )


def command(c, text, sender, message_id, now):
    undo = re.fullmatch(r"undo\s+(P[A-Fa-f0-9]{6})", text.strip(), re.I)
    if undo:
        key = "undo-memory:" + undo[1].upper()
        raw = c.execute(select(db.state.c.value).where(db.state.c.key == key)).scalar()
        entry = json.loads(raw) if raw else None
        if not entry or entry["sender"] != sender or entry["expires_at"] <= now:
            raise ValueError("That undo is unavailable for your account or has expired")
        current = (
            c.execute(
                select(db.preferences).where(
                    db.preferences.c.subject == entry["subject"],
                    db.preferences.c.kind == entry["kind"],
                )
            )
            .mappings()
            .first()
        )
        if (dict(current) if current else None) != entry["after"]:
            raise ValueError("Your preference has changed again; ask for a correction instead")
        c.execute(
            delete(db.preferences).where(
                db.preferences.c.subject == entry["subject"],
                db.preferences.c.kind == entry["kind"],
            )
        )
        if entry["before"]:
            c.execute(insert(db.preferences).values(**entry["before"]))
        c.execute(delete(db.state).where(db.state.c.key == key))
        return "Undone — your previous preference is restored."
    match = re.fullmatch(
        r"(?:trip\s+)?(yes|confirm|save|no|discard)\s+([PLIEF][A-Fa-f0-9]{6})", text.strip(), re.I
    )
    if match:
        return decide(c, match[2], sender, match[1].lower() in {"yes", "confirm", "save"}, now)
    if text.strip().lower() in {"trip family", "who is in the family", "show family members"}:
        people = directory(c)
        return "Family directory:\n" + "\n".join(
            f"• {p['name']} — "
            + ("Telegram-linked" if p["telegram_linked"] else "reported companion, unlinked")
            + f" [{p['member_id']}]"
            for p in people
        )
    return None
