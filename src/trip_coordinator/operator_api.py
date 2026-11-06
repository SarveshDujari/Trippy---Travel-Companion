"""Loopback-only demo adapter. No endpoint can approve, book, or take payment."""

import hmac
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import storage as db
from .features import trigger_delay


class DelayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    minutes: int = Field(ge=1, le=360, strict=True)


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
                "agents": [
                    "Coordinator: routing and validation",
                    "Planner: advice and tools",
                    "Reservation: approved simulated actions",
                ],
            }

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
