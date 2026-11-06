"""Read-only native Switch identity lookup; run with Switch's Python environment.

The supported API supplies agent IDs and verified linked account claims. This
local-only diagnostic joins those exact IDs to their transport client records,
without deriving an identity from a display name or rewriting Switch data.
"""

import asyncio
import json
import os
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]


async def main():
    local = Path(os.environ["APPDATA"]) / "switchdash/local-switch-server"
    env = dict(
        line.split("=", 1)
        for line in (local / "native/run/switch-core.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    discovery = json.loads((ROOT / "config/switch-discovery.local.json").read_text())
    linked = json.loads((ROOT / "config/linked-users.local.json").read_text())
    verified = [
        u
        for u in linked["users"]
        if any(x["user_id"] == linked["owner_id"] for x in u["claimed_by"])
    ]
    if len(verified) != 1:
        raise RuntimeError("Expected exactly one verified linked Telegram account")
    agents = {
        a["name"]: a["id"]
        for a in discovery["agents"]
        if a["name"]
        in {
            "trip-coordinator",
            "trip-planner",
            "trip-reservations",
            "trip-researcher",
            "trip-mediator",
            "trip-watcher",
        }
    }
    conn = await asyncpg.connect(
        host=env["DB_HOST"],
        port=int(env["DB_PORT"]),
        user=env["DB_OWNER_USER"],
        password=env["DB_OWNER_PASSWORD"],
        database=env["DB_NAME"],
    )
    try:
        async with conn.transaction(readonly=True):
            results = await conn.fetch(
                "SELECT a.id,c.matrix_user_id FROM agents a JOIN clients c "
                "ON c.id=a.client_id AND c.tenant_id=a.tenant_id WHERE a.id=ANY($1::text[])",
                list(agents.values()),
            )
            by_id = {r["id"]: r["matrix_user_id"] for r in results}
            human = await conn.fetchval(
                "SELECT c.matrix_user_id FROM external_users e JOIN clients c "
                "ON c.id=e.client_id AND c.tenant_id=e.tenant_id "
                "WHERE e.id=$1 AND e.bridge_id=$2",
                verified[0]["id"],
                discovery["room"]["bridge_id"],
            )
        if human is None or len(by_id) != len(agents):
            raise RuntimeError("Identity mapping is incomplete")
        config_path = ROOT / "config/runtime.local.json"
        existing = json.loads(config_path.read_text()) if config_path.exists() else {}
        members = existing.get("members") or [{"participant_id": human, "can_approve": True}]
        if human not in {m["participant_id"] for m in members}:
            raise RuntimeError(
                "Verified owner differs from existing family membership; review mapping"
            )
        config = {
            **existing,
            "bridge_url": discovery["bridge_url"],
            "room_id": discovery["room"]["id"],
            "bridge_id": discovery["room"]["bridge_id"],
            "coordinator_agent_id": agents["trip-coordinator"],
            "planner_participant_id": by_id[agents["trip-planner"]],
            "reservation_participant_id": by_id[agents["trip-reservations"]],
            "members": members,
            "database_path": existing.get("database_path", "data/telegram-demo.db"),
            "weather_enabled": existing.get("weather_enabled", False),
        }
        for role in ["researcher", "mediator", "watcher"]:
            if "trip-" + role in agents:
                config[role + "_participant_id"] = by_id[agents["trip-" + role]]
        (ROOT / "config/runtime.local.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )
        (ROOT / "config/coordinator-identity.local.json").write_text(
            json.dumps({"participant_id": by_id[agents["trip-coordinator"]]}), encoding="utf-8"
        )
        print("Saved verified transport identities and private runtime configuration.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
