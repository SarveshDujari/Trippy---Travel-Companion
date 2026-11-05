"""Local operator CLI. No public API accepts user or agent identity claims."""

import argparse
import asyncio
import json
import logging
import os
import time
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text, update

from . import discussion
from . import storage as db
from .contracts import Message, RuntimeConfig
from .runtime import run, worker_lock
from .service import Coordinator, seed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init",
            "run",
            "inspect",
            "resume",
            "retry-message",
            "delay",
            "calendar",
            "compare",
            "trace",
            "serve-demo",
        ],
    )
    parser.add_argument(
        "--event-id", help="Stable simulated disruption ID; retries are deduplicated"
    )
    parser.add_argument("--minutes", type=int, default=120)
    parser.add_argument("--output", default="data/hyderabad-demo.ics")
    parser.add_argument("--operator-token-file", default="config/demo-token.local.json")
    parser.add_argument("--config", default="config/runtime.local.json")
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--message-id", type=int)
    parser.add_argument(
        "--key-file", help="Ignored local JSON file holding Coordinator credentials"
    )
    parser.add_argument(
        "--demo-start",
        action="store_true",
        help="Start one explicitly simulated, operator-requested planning task",
    )
    parser.add_argument("--demo-chat", help="Operator-started test question; not a family message")
    args = parser.parse_args()
    config = RuntimeConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
    engine = db.make_engine(config.database_path)
    with (
        nullcontext()
        if args.action in {"inspect", "trace", "calendar", "compare", "delay", "serve-demo"}
        else worker_lock(config.database_path)
    ):
        if args.action == "init":
            migration = Config("alembic.ini")
            migration.set_main_option("trip_database_path", config.database_path)
            command.upgrade(migration, "head")
            seed(engine)
            print("Initialized FICTIONAL Hyderabad fixture. No external messages sent.")
        elif args.action == "run":
            if "REPLACE_WITH_" in config.model_dump_json():
                raise SystemExit(
                    "Fill verified room/participant IDs in a local runtime config first"
                )
            key = os.environ.get("TRIP_SWITCH_API_KEY")
            if args.key_file:
                secrets = json.loads(Path(args.key_file).read_text(encoding="utf-8"))
                if secrets.get("coordinator_agent_id") != config.coordinator_agent_id:
                    raise SystemExit("Credential identity does not match runtime configuration")
                key = secrets["coordinator_api_key"]
            if not key:
                raise SystemExit("Set TRIP_SWITCH_API_KEY privately before starting the worker")
            logging.basicConfig(level=logging.WARNING)
            if args.demo_start:
                with engine.begin() as c:
                    Coordinator(engine, config)._start(
                        c,
                        "SIMULATED operator-started Telegram test: repair delayed arrival",
                        int(time.time()),
                    )
            if args.demo_chat:
                now = int(time.time())
                with engine.begin() as c:
                    discussion.queue(
                        c,
                        Message(
                            sender="operator-demo",
                            message_id="operator-demo-" + uuid4().hex,
                            body=args.demo_chat,
                            timestamp=now * 1000,
                        ),
                        now,
                    )
            asyncio.run(run(engine, config, key))
        elif args.action == "serve-demo":
            import secrets

            import uvicorn

            from .operator_api import create_app

            token_file = Path(args.operator_token_file)
            if not token_file.exists():
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(
                    json.dumps({"token": secrets.token_urlsafe(32)}), encoding="utf-8"
                )
            token = json.loads(token_file.read_text(encoding="utf-8"))["token"]
            uvicorn.run(create_app(engine, config, token), host="127.0.0.1", port=8765)
        elif args.action == "delay":
            from .features import trigger_delay

            if not args.event_id:
                raise SystemExit(
                    "--event-id is required so event retries cannot duplicate the delay"
                )
            with engine.begin() as c:
                changed = trigger_delay(c, config, args.event_id, args.minutes, int(time.time()))
            print(
                "SIMULATED delay queued for planning and human approval."
                if changed
                else "Event already processed."
            )
        elif args.action == "calendar":
            from .features import calendar

            with engine.connect() as c:
                print(calendar(c, args.output))
        elif args.action == "compare":
            from .features import demo_conflict

            with engine.connect() as c:
                print(json.dumps(demo_conflict(c), indent=2, ensure_ascii=False))
        elif args.action == "trace":
            with engine.connect() as c:
                print(
                    json.dumps(
                        {
                            "rooms": {
                                "family": config.room_id,
                                "private_agent_work": config.work_room_id,
                            },
                            "tasks": [dict(x) for x in c.execute(select(db.tasks)).mappings()],
                            "discussions": [
                                dict(x)
                                for x in c.execute(
                                    select(
                                        db.discussions.c.id,
                                        db.discussions.c.status,
                                        db.discussions.c.assignment_id,
                                    )
                                ).mappings()
                            ],
                            "tools": [
                                dict(x)
                                for x in c.execute(
                                    select(db.tools.c.id, db.tools.c.name, db.tools.c.status)
                                ).mappings()
                            ],
                            "delivery_counts": [
                                dict(x)
                                for x in c.execute(
                                    text(
                                        "SELECT status, count(*) AS count "
                                        "FROM outbox GROUP BY status"
                                    )
                                ).mappings()
                            ],
                        },
                        indent=2,
                    )
                )
        elif args.action == "inspect":
            with engine.connect() as c:
                print(
                    json.dumps(
                        {
                            "tasks": [dict(x) for x in c.execute(select(db.tasks)).mappings()],
                            "deliveries": [
                                dict(x)
                                for x in c.execute(
                                    select(
                                        db.outbox.c.id,
                                        db.outbox.c.status,
                                        db.outbox.c.attempts,
                                        db.outbox.c.last_error,
                                    )
                                ).mappings()
                            ],
                        },
                        indent=2,
                    )
                )
        else:
            if not args.reviewed:
                raise SystemExit("Inspect room history and pending approvals; pass --reviewed")
            with engine.begin() as c:
                if args.action == "resume":
                    # Do not replay historical human approval messages after an unfillable gap.
                    c.execute(delete(db.state).where(db.state.c.key.in_(["blocked", "cursor"])))
                    c.execute(
                        update(db.tasks)
                        .where(
                            ~db.tasks.c.state.in_(["completed", "partially_completed", "failed"])
                        )
                        .values(state="failed")
                    )
                    c.execute(
                        update(db.outbox)
                        .where(db.outbox.c.status.in_(["pending", "sending", "unknown"]))
                        .values(status="failed")
                    )
                    print("Continuity reset. Active tasks invalidated; send fresh trip commands.")
                else:
                    if args.message_id is None:
                        raise SystemExit("--message-id is required")
                    count = c.execute(
                        update(db.outbox)
                        .where(
                            db.outbox.c.id == args.message_id,
                            db.outbox.c.status.in_(["unknown", "failed"]),
                        )
                        .values(status="pending", attempts=0, next_attempt=0)
                    ).rowcount
                    print(f"Requeued {count} reviewed message(s); duplicate delivery is possible.")


if __name__ == "__main__":
    main()
