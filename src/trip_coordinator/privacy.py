"""Audience policy and bounded conversation retention, independent of the model."""

import re

from sqlalchemy import delete, select, update

from . import storage as db

SENSITIVE_KINDS = {"senior", "wheelchair", "fatigue_limit", "max_walk_m"}
SENSITIVE = re.compile(
    r"\b(?:expense|expenses|spent|split.{0,20}bill|senior|wheelchair|fatigue|"
    r"medical|diagnos\w*|medication|walking limit|max_walk_m|fatigue_limit)\b",
    re.I,
)
NOTICE = (
    "This group response may expose personal health, mobility or expense details. "
    "I have not processed or repeated them. If you want this request shared here, "
    "send it again starting with 'Share with group:'. Private-message delivery "
    "is not configured. Other group members may already see your original message."
)


def shared(text):
    return text.casefold().startswith("share with group:")


def restricted(text, config):
    return config.group_privacy and not shared(text) and bool(SENSITIVE.search(text))


def policy(days):
    return (
        f"Privacy and retention\nConversation text, attachments in local context, tool "
        f"jobs and comparison records expire after {days} days during worker maintenance. "
        "Saved preferences, explicit facts and reviewed itinerary imports persist until "
        "corrected or removed. Booking decisions, expense records and audit history are "
        "separate retained records. 'Forget my memory' asks for confirmation before "
        "clearing local discussion memory and preferences reported by you.\n"
        "Personal mobility/health and expense requests require 'Share with group:' "
        "before group processing. The bot cannot erase Telegram/Switch history, backups "
        "or media held by those services. This is a local retention policy, not a "
        "guarantee of erasure from every copy. No private-message route is configured."
    )


def expire(c, now, days):
    old = list(
        c.execute(
            select(db.discussions).where(
                db.discussions.c.created_at < now - days * 86400,
                db.discussions.c.status.in_(["completed", "cancelled", "expired", "failed"]),
                db.discussions.c.request != "[expired conversation]",
            )
        ).mappings()
    )
    for row in old:
        identity = row["id"]
        c.execute(delete(db.chat).where(db.chat.c.discussion_id == identity))
        c.execute(delete(db.tools).where(db.tools.c.discussion_id == identity))
        c.execute(delete(db.comparisons).where(db.comparisons.c.discussion_id == identity))
        c.execute(
            update(db.outbox)
            .where(
                (db.outbox.c.related_id == identity)
                | db.outbox.c.dedupe_key.in_(
                    [identity + ":reply", identity + ":visual", row["assignment_id"] or ""]
                )
            )
            .values(
                body="[expired conversation]",
                media_json=None,
                attachment_text=None,
                status="cancelled",
            )
        )
        c.execute(
            update(db.discussions)
            .where(db.discussions.c.id == identity)
            .values(
                request="[expired conversation]",
                context="{}",
                attachments_json="[]",
                specialist_notes="{}",
                status="expired",
            )
        )
    return len(old)
