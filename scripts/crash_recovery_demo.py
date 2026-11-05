"""Actually terminate a child mid-simulator transaction, then replay and verify.

Uses a temporary database and scripted operator fixtures. Never touches Telegram.
External delivery uncertainty is deliberately not claimed to be exactly-once.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text

from trip_coordinator import storage as db
from trip_coordinator.contracts import PlanResult, RuntimeConfig
from trip_coordinator.service import Coordinator, seed


def child(folder):
    config = RuntimeConfig.model_validate_json((folder / "config.json").read_text())
    engine = db.make_engine(config.database_path)

    @event.listens_for(engine, "after_cursor_execute")
    def interrupt(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO actions"):
            os._exit(73)  # A real abrupt process death with an open transaction.

    Coordinator(engine, config).consume(json.loads((folder / "event.json").read_text()))
    raise RuntimeError("Crash injection did not run")


def run_demo():
    with tempfile.TemporaryDirectory(prefix="trippy-crash-") as temp:
        folder = Path(temp)
        config = RuntimeConfig(
            bridge_url="http://127.0.0.1:8000",
            room_id="demo",
            bridge_id="demo",
            coordinator_agent_id="demo-coordinator",
            planner_participant_id="planner",
            reservation_participant_id="reservations",
            members=[{"participant_id": "operator"}],
            database_path=str(folder / "trip.db"),
        )
        migration = Config("alembic.ini")
        migration.set_main_option("trip_database_path", config.database_path)
        command.upgrade(migration, "head")
        engine = db.make_engine(config.database_path)
        seed(engine)
        now = int(time.time())
        coordinator = Coordinator(engine, config)
        with engine.begin() as c:
            coordinator._start(c, "SIMULATED isolated crash exercise", now)
            task = c.execute(select(db.tasks)).mappings().one()
            plan = PlanResult.model_validate(
                {
                    "kind": "trip_plan",
                    "task_id": task["id"],
                    "assignment_id": task["assignment_id"],
                    "trip_version": 1,
                    "summary": "Crash exercise",
                    "changes": [
                        {
                            "item_id": "transfer",
                            "start": "2026-11-06T20:15:00+05:30",
                            "extra_cost_minor": 30000,
                        },
                        {
                            "item_id": "hotel",
                            "start": "2026-11-06T21:00:00+05:30",
                            "extra_cost_minor": 0,
                        },
                        {
                            "item_id": "dinner",
                            "start": "2026-11-06T21:45:00+05:30",
                            "extra_cost_minor": 20000,
                            "alternative": "room_service",
                        },
                    ],
                }
            )
            coordinator._plan(c, plan, now)
            proposal = c.execute(select(db.proposals)).mappings().one()
            coordinator._approve(c, proposal["id"], "operator", now)
            task = c.execute(select(db.tasks)).mappings().one()
        envelope = {
            "type": "message",
            "room_id": "demo",
            "bridge_id": "demo",
            "sequence": 1,
            "payload": {
                "sender": "reservations",
                "message_id": "crash-execution",
                "timestamp": now * 1000,
                "body": json.dumps(
                    {
                        "kind": "trip_execute",
                        "task_id": task["id"],
                        "assignment_id": task["assignment_id"],
                        "proposal_id": proposal["id"],
                    }
                ),
            },
        }
        (folder / "config.json").write_text(config.model_dump_json())
        (folder / "event.json").write_text(json.dumps(envelope))
        process = subprocess.run([sys.executable, __file__, "--child", str(folder)], check=False)
        assert process.returncode == 73, "Child did not die at the intended point"
        with engine.connect() as c:
            assert not c.execute(select(db.actions)).all(), "Partial transaction survived crash"
        restarted = Coordinator(engine, config)
        restarted.consume(envelope)
        restarted.consume(envelope)
        with engine.connect() as c:
            assert len(c.execute(select(db.actions)).all()) == 3
            assert c.execute(select(db.tasks.c.state)).scalar() == "completed"
            assert (
                len(
                    c.execute(
                        select(db.outbox).where(
                            db.outbox.c.dedupe_key == proposal["id"] + ":outcome"
                        )
                    ).all()
                )
                == 1
            )
            assert c.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        engine.dispose()
        print(
            "PASS: killed mid-transaction; replay produced exactly three simulator actions "
            "and one durable outcome. No external messages sent."
        )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]))
    else:
        run_demo()
