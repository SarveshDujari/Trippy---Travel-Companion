"""Single-worker durable runtime; all chat input comes from Switch, not an HTTP webhook."""

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import insert, select, update

from . import discussion
from . import storage as db
from .service import Coordinator, enqueue
from .switch import ProtocolStopped, SwitchClient
from .weather import forecast, observe

log = logging.getLogger(__name__)


@contextmanager
def worker_lock(database_path):
    """OS lock releases on process exit, including a crash; no stale lock takeover."""
    path = Path(database_path).with_suffix(".worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another worker owns this database") from None
    try:
        yield
    finally:
        handle.close()


def binding(engine, config):
    # Reusing a DB against a different room/identity can misattribute approvals.
    raw = config.model_dump(exclude={"approval_ttl_seconds", "assignment_ttl_seconds"})
    optional = {
        "researcher_participant_id",
        "mediator_participant_id",
        "watcher_participant_id",
        "watcher_quiet_start",
        "watcher_quiet_end",
    }
    base_raw = {k: v for k, v in raw.items() if k not in optional}
    if not any(
        [
            config.researcher_participant_id,
            config.mediator_participant_id,
            config.watcher_participant_id,
        ]
    ):
        raw = base_raw
    fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    with engine.begin() as c:
        saved = dict(c.execute(select(db.state.c.key, db.state.c.value)).all())
        if saved.get("binding") not in {None, fingerprint}:
            base_hash = hashlib.sha256(json.dumps(base_raw, sort_keys=True).encode()).hexdigest()
            active_work = c.execute(
                select(db.discussions.c.id).where(
                    db.discussions.c.status.in_(["queued", "waiting", "tool_pending"])
                )
            ).first()
            active_task = c.execute(
                select(db.tasks.c.id).where(
                    ~db.tasks.c.state.in_(
                        ["completed", "partially_completed", "failed", "cancelled"]
                    )
                )
            ).first()
            if saved["binding"] == base_hash and not active_work and not active_task:
                Coordinator._state(c, "binding", fingerprint)
                saved["binding"] = fingerprint
        if saved.get("binding") not in {None, fingerprint}:
            # Upgrade only a legacy binding with precisely the same original
            # server, family room, roles, and members. New private-room routing
            # and conversation fields did not exist in that format.
            legacy = config.model_dump(
                exclude={
                    "approval_ttl_seconds",
                    "assignment_ttl_seconds",
                    "work_room_id",
                    "conversation_enabled",
                }
                | optional
            )
            legacy_hash = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
            if saved.get("binding_format") is None and saved["binding"] == legacy_hash:
                Coordinator._state(c, "binding", fingerprint)
            else:
                raise RuntimeError("Runtime config changed; use a separate DB or review migration")
        if "binding" not in saved:
            c.execute(insert(db.state).values(key="binding", value=fingerprint))
        Coordinator._state(c, "binding_format", "2")
        if "connection_id" not in saved:
            saved["connection_id"] = str(uuid4())
            c.execute(insert(db.state).values(key="connection_id", value=saved["connection_id"]))
        if saved.get("blocked"):
            raise ProtocolStopped("Runtime is blocked; review Switch continuity before resume")
        return saved


async def dispatch_once(engine, client, room_id, now=None):
    now = int(time.time()) if now is None else now
    with engine.begin() as c:
        # A crash after dispatch began has an uncertain delivery outcome.
        c.execute(
            update(db.outbox)
            .where(db.outbox.c.status == "sending")
            .values(status="unknown", last_error="Worker restarted during send")
        )
        item = (
            c.execute(
                select(db.outbox)
                .where(
                    db.outbox.c.status == "pending",
                    db.outbox.c.next_attempt <= now,
                )
                .order_by(db.outbox.c.id)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if item is None:
            return
        item = dict(item)
        c.execute(
            update(db.outbox)
            .where(db.outbox.c.id == item["id"])
            .values(status="sending", attempts=item["attempts"] + 1)
        )
    status, error, retry = "sent", None, now
    try:
        # Stable reference allows humans/agents to recognize an uncertain resend.
        suffix = f"\n[ref:{item['dedupe_key']}]" if item["show_reference"] else ""
        if item.get("media_json"):
            await client.upload_media(
                item["room_id"] or room_id, json.loads(item["media_json"]), item["body"]
            )
        elif item.get("attachment_text"):
            await client.upload_calendar(
                item["room_id"] or room_id, item["attachment_text"], item["body"]
            )
        else:
            await client.post(item["room_id"] or room_id, item["body"] + suffix)
    except httpx.ConnectError:
        status, error = "pending", "Connection unavailable"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            status, error = "pending", "Switch rate limit"
        elif 400 <= exc.response.status_code < 500:
            status, error = "failed", f"Switch rejected request ({exc.response.status_code})"
        else:
            status, error = "unknown", "Switch response did not establish delivery"
    except httpx.TransportError:
        status, error = "unknown", "Delivery outcome uncertain; inspect room before retry"
    except ValueError:
        status, error = "failed", "Image could not be validated or delivered"
    if status == "pending":
        status = "failed" if item["attempts"] + 1 >= 5 else "pending"
        retry = now + min(60, 2 ** (item["attempts"] + 1))
    with engine.begin() as c:
        c.execute(
            update(db.outbox)
            .where(db.outbox.c.id == item["id"])
            .where(db.outbox.c.status == "sending")
            .values(status=status, next_attempt=retry, last_error=error)
        )
        if status == "failed" and item.get("media_json") and item.get("related_id"):
            c.execute(
                update(db.discussions)
                .where(db.discussions.c.id == item["related_id"])
                .values(status="failed")
            )
            enqueue(
                c,
                item["dedupe_key"] + ":media-failed",
                "I couldn't inspect that image. "
                "Please send a smaller, clear PNG or JPEG screenshot.",
                now,
                show_reference=False,
            )


async def run(engine, config, key):
    if config.conversation_enabled and not config.work_room_id:
        raise RuntimeError("Conversational mode requires a private Switch workroom")
    saved = binding(engine, config)
    coordinator = Coordinator(engine, config)
    client = SwitchClient(config.bridge_url, config.coordinator_agent_id, key)
    connected = asyncio.Event()

    async def prepare_discussion():
        if not config.conversation_enabled:
            return
        now = int(time.time())
        with engine.begin() as c:
            expired = (
                c.execute(
                    select(db.discussions.c.id).where(
                        ((db.discussions.c.status == "waiting") & (db.discussions.c.deadline < now))
                        | (
                            db.discussions.c.status.in_(["queued", "tool_pending"])
                            & (db.discussions.c.created_at < now - 600)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for identity in expired:
                c.execute(
                    update(db.discussions)
                    .where(db.discussions.c.id == identity)
                    .values(status="failed")
                )
                assigned = c.execute(
                    select(db.discussions.c.assigned_role).where(db.discussions.c.id == identity)
                ).scalar()
                if assigned == "watcher":
                    continue
                enqueue(
                    c,
                    identity + ":expired",
                    "I couldn't finish that reply in time. Please send your question again.",
                    now,
                    show_reference=False,
                )
            if c.execute(
                select(db.discussions.c.id).where(db.discussions.c.status == "waiting")
            ).first():
                return
            pending = (
                c.execute(
                    select(db.discussions)
                    .join(db.chat, db.chat.c.discussion_id == db.discussions.c.id)
                    .where(db.discussions.c.status == "queued", db.chat.c.role == "human")
                    .order_by(db.chat.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if pending is None:
            return
        weather_data = {"status": "not_requested", "checked_at": ""}
        with engine.begin() as c:
            current = (
                c.execute(
                    select(db.discussions).where(
                        db.discussions.c.id == pending["id"], db.discussions.c.status == "queued"
                    )
                )
                .mappings()
                .first()
            )
            if current is not None:
                discussion.dispatch(c, current, config, weather_data, int(time.time()))

    async def process_tool():
        from .travel_tools import execute

        with engine.begin() as c:
            job = (
                c.execute(
                    select(db.tools)
                    .where(db.tools.c.status == "queued")
                    .order_by(db.tools.c.created_at)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if job is None:
                return
            c.execute(update(db.tools).where(db.tools.c.id == job["id"]).values(status="running"))
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as http:
            try:
                async with asyncio.timeout(90 if job["name"] == "compare_plans" else 25):
                    result = await execute(
                        engine,
                        http,
                        job["name"],
                        json.loads(job["arguments"]),
                        discussion_id=job["discussion_id"],
                    )
            except TimeoutError:
                result = {
                    "status": "unavailable",
                    "data": {},
                    "source_url": "",
                    "retrieved_at": int(time.time()),
                    "reason": "Tool time budget exhausted",
                }
        with engine.begin() as c:
            current = c.execute(
                select(db.discussions.c.status).where(db.discussions.c.id == job["discussion_id"])
            ).scalar()
            if current == "tool_pending":
                c.execute(
                    update(db.tools)
                    .where(db.tools.c.id == job["id"])
                    .values(status="done", result=json.dumps(result))
                )
                c.execute(
                    update(db.discussions)
                    .where(db.discussions.c.id == job["discussion_id"])
                    .values(status="queued")
                )

    async def connection_ready(state):
        with engine.begin() as c:
            if not c.execute(select(db.state.c.key).where(db.state.c.key == "cursor")).first():
                Coordinator._state(c, "cursor", str(state.get("cursor", 0)))
            Coordinator._state(c, "connected_at", str(int(time.time())))
        connected.set()

    async def receive(envelope):
        coordinator.consume(envelope)

    async def maintenance():
        with engine.begin() as c:
            # Retrying an interrupted read-only external tool is safe.
            c.execute(
                update(db.tools).where(db.tools.c.status == "running").values(status="queued")
            )
        next_weather_check = 0
        while True:
            await connected.wait()
            coordinator.expire()
            await dispatch_once(engine, client, config.room_id)
            await process_tool()
            await prepare_discussion()
            with engine.connect() as c:
                morning = c.execute(
                    select(db.state.c.value).where(db.state.c.key == "morning_schedule")
                ).scalar()
            if morning:
                from .features import maybe_briefing

                with engine.connect() as c:
                    sent_today = c.execute(
                        select(db.state.c.value).where(db.state.c.key == "last_briefing")
                    ).scalar()
                from datetime import datetime
                from zoneinfo import ZoneInfo

                today = datetime.now(ZoneInfo("Asia/Kolkata"))
                if (
                    today.strftime("%H:%M") >= morning
                    and sent_today != "briefing:" + today.date().isoformat()
                ):
                    async with httpx.AsyncClient(timeout=8) as http:
                        data = await forecast(engine, http)
                    await maybe_briefing(engine, config, data, int(time.time()))
            if config.weather_enabled and time.time() >= next_weather_check:
                next_weather_check = time.time() + 900
                try:
                    async with httpx.AsyncClient(timeout=15) as weather_client:
                        await observe(engine, weather_client, config=config)
                except (httpx.HTTPError, ValueError, KeyError):
                    log.warning("Weather check unavailable; no trip changes made")
            await asyncio.sleep(1)

    async def incoming():
        attempt = 0
        while attempt < 5:
            connected.clear()
            started = time.time()
            with engine.connect() as c:
                cursor = c.execute(
                    select(db.state.c.value).where(db.state.c.key == "cursor")
                ).scalar()
            try:
                await client.listen(
                    saved["connection_id"],
                    config.room_id,
                    int(cursor) if cursor is not None else None,
                    receive,
                    connection_ready,
                    room_ids=[config.room_id, config.work_room_id] if config.work_room_id else None,
                )
            except ProtocolStopped:
                raise
            except (httpx.TransportError, httpx.HTTPStatusError):
                log.warning("Switch disconnected; reconnect attempt %s/5", attempt + 1)
            if connected.is_set() and time.time() - started >= 30:
                attempt = 0
            else:
                attempt += 1
            await asyncio.sleep(min(30, 2**attempt))
        raise ProtocolStopped("Switch reconnect budget exhausted")

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(incoming())
            group.create_task(maintenance())
    except* ProtocolStopped:
        with engine.begin() as c:
            Coordinator._state(c, "blocked", "Switch continuity requires operator review")
        raise
    finally:
        await client.close()
