"""Loopback-only demo adapter. No endpoint can approve, book, or take payment."""

import hmac
import json
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import storage as db
from .approvals import card as approval_card
from .features import trigger_delay


class DelayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    minutes: int = Field(ge=1, le=360, strict=True)


class DemoMode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


def create_app(engine, config, token):
    if len(token) < 32:
        raise ValueError("Use a private random operator token of at least 32 characters")

    def authorize(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "Operator authorization required")

    app = FastAPI(title="Trippy simulated disruption API")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        from .dashboard import PAGE

        return PAGE

    @app.get("/dashboard/state", dependencies=[Depends(authorize)])
    def dashboard_state():
        with engine.connect() as c:

            def rows(table, columns, limit=30):
                return [
                    dict(x)
                    for x in c.execute(
                        select(*(table.c[k] for k in columns)).limit(limit)
                    ).mappings()
                ]

            return {
                "mode": "SIMULATED BOOKINGS • live tool evidence when available",
                "itinerary": rows(db.items, ["id", "start", "status"]),
                "dependencies": rows(db.edges, ["parent", "child", "buffer_minutes"]),
                "tasks": rows(db.tasks, ["id", "state", "proposal_id"]),
                "tools": [
                    dict(x)
                    for x in c.execute(
                        select(db.tools.c.id, db.tools.c.name, db.tools.c.status)
                        .order_by(db.tools.c.id.desc())
                        .limit(30)
                    ).mappings()
                ],
                "delivery": rows(db.outbox, ["id", "status", "attempts"]),
                "ledger": [
                    {
                        "id": p["id"],
                        "task_id": p["task_id"],
                        "expires_at": p["expires_at"],
                        "approved_by": p["approved_by"],
                        "card": approval_card(c, p, int(time.time())),
                    }
                    for p in c.execute(
                        select(db.proposals).order_by(db.proposals.c.expires_at.desc()).limit(100)
                    ).mappings()
                ],
                "evidence": [
                    {"id": job["id"], "tool": job["name"], "result": json.loads(job["result"])}
                    for job in c.execute(
                        select(db.tools)
                        .where(db.tools.c.result.is_not(None))
                        .order_by(db.tools.c.created_at.desc())
                        .limit(100)
                    ).mappings()
                ],
                "verification": [
                    dict(x)
                    for x in c.execute(
                        select(db.events)
                        .where(db.events.c.kind == "verification_passed")
                        .order_by(db.events.c.created_at.desc())
                        .limit(100)
                    ).mappings()
                ],
                "cached_demo": c.execute(
                    select(db.state.c.value).where(db.state.c.key == "cached-demo-mode")
                ).scalar()
                == "true",
                "agents": [
                    "Coordinator: routing and validation",
                    "Planner: advice and tools",
                    "Reservation: approved simulated actions",
                ]
                + [
                    f"{role.title()}: configured specialist"
                    for role in ["researcher", "mediator", "watcher"]
                    if getattr(config, role + "_participant_id")
                ],
                "specialists": rows(db.discussions, ["id", "assigned_role", "status"]),
            }

    @app.post("/demo-mode", dependencies=[Depends(authorize)])
    def demo_mode(mode: DemoMode):
        from .service import Coordinator

        with engine.begin() as c:
            Coordinator._state(c, "cached-demo-mode", json.dumps(mode.enabled))
        return {
            "cached_demo": mode.enabled,
            "note": "Read-only tools use historical cache only when enabled",
        }

    @app.get("/dashboard/ledger", dependencies=[Depends(authorize)])
    def ledger(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with engine.connect() as c:
            entries = [
                {"id": p["id"], "card": approval_card(c, p, int(time.time()))}
                for p in c.execute(
                    select(db.proposals)
                    .order_by(db.proposals.c.expires_at.desc(), db.proposals.c.id)
                    .offset(offset)
                    .limit(limit)
                ).mappings()
            ]
        return {"offset": offset, "entries": entries, "next_offset": offset + len(entries)}

    @app.post("/disruptions/flight-delay", dependencies=[Depends(authorize)])
    def delay(event: DelayEvent):
        try:
            with engine.begin() as c:
                changed = trigger_delay(c, config, event.event_id, event.minutes, int(time.time()))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"mode": "SIMULATED", "processed": changed, "approval_required": True}

    @app.get("/trace", dependencies=[Depends(authorize)])
    def trace():
        with engine.connect() as c:
            return {
                "mode": "SIMULATED",
                "tasks": [
                    dict(x) for x in c.execute(select(db.tasks.c.id, db.tasks.c.state)).mappings()
                ],
                "tools": [
                    dict(x)
                    for x in c.execute(
                        select(db.tools.c.id, db.tools.c.name, db.tools.c.status)
                    ).mappings()
                ],
            }

    return app
