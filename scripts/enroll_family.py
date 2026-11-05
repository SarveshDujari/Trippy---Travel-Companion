"""Enroll a verified Switch-linked account without granting spending permission.

Run with the installed native Switch Python (it supplies asyncpg). --list is
read-only. --enroll RECORD_ID requires the Coordinator to be stopped and reviews
the existing binding before updating the ignored local config and family roster.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def main():
    import asyncpg
    from sqlalchemy import insert, select

    from trip_coordinator import storage as db
    from trip_coordinator.contracts import RuntimeConfig
    from trip_coordinator.family import member_id, sync_members
    from trip_coordinator.runtime import worker_lock
    from trip_coordinator.service import Coordinator

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--enroll", help="Exact verified external-user record ID from --list")
    args = parser.parse_args()
    local = Path(os.environ["APPDATA"]) / "switchdash/local-switch-server"
    env = dict(
        x.split("=", 1)
        for x in (local / "native/run/switch-core.env").read_text().splitlines()
        if "=" in x and not x.startswith("#")
    )
    path = ROOT / "config/runtime.local.json"
    original = path.read_text(encoding="utf-8")
    config = RuntimeConfig.model_validate_json(original)
    with httpx.Client(base_url=config.bridge_url, timeout=15, trust_env=False) as client:
        client.post(
            "/gateway/auth/login",
            json={"email": env["GATEWAY_ADMIN_EMAIL"], "password": env["GATEWAY_ADMIN_PASSWORD"]},
        ).raise_for_status()
        users = (
            client.get(f"/gateway/collaborations/{config.bridge_id}/users")
            .raise_for_status()
            .json()
        )
    verified = [u for u in users if u.get("claimed_by")]
    if not args.enroll:
        print(
            json.dumps(
                [
                    {
                        "record_id": u["id"],
                        "name": u.get("external_username"),
                        "switch_link_verified": True,
                    }
                    for u in verified
                ],
                indent=2,
            )
        )
        return
    selected = next((u for u in verified if u["id"] == args.enroll), None)
    if not selected:
        raise SystemExit(
            "Choose an exact verified record from --list; names are not authentication"
        )
    conn = await asyncpg.connect(
        host=env["DB_HOST"],
        port=int(env["DB_PORT"]),
        user=env["DB_OWNER_USER"],
        password=env["DB_OWNER_PASSWORD"],
        database=env["DB_NAME"],
    )
    try:
        async with conn.transaction(readonly=True):
            participant = await conn.fetchval(
                "SELECT c.matrix_user_id FROM external_users e JOIN clients c "
                "ON c.id=e.client_id AND c.tenant_id=e.tenant_id WHERE e.id=$1 AND e.bridge_id=$2",
                selected["id"],
                config.bridge_id,
            )
    finally:
        await conn.close()
    if not participant:
        raise SystemExit("Switch has no transport identity for this linked record")
    if participant in {p.participant_id for p in config.members}:
        print("That verified member is already enrolled; permissions were not changed.")
        return
    if len(config.members) >= 12:
        raise SystemExit("The family roster limit is twelve")
    updated = config.model_dump()
    updated["members"].append({"participant_id": participant, "can_approve": False})
    revised = RuntimeConfig.model_validate(updated)

    def fingerprint(value):
        raw = value.model_dump(exclude={"approval_ttl_seconds", "assignment_ttl_seconds"})
        return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()

    engine = db.make_engine(str(ROOT / config.database_path))
    try:
        with worker_lock(str(ROOT / config.database_path)), engine.begin() as c:
            binding = c.execute(
                select(db.state.c.value).where(db.state.c.key == "binding")
            ).scalar()
            if binding not in {None, fingerprint(config)}:
                raise SystemExit(
                    "The existing runtime binding differs; resolve it before enrollment"
                )
            sync_members(c, revised, int(time.time()))
            Coordinator._state(c, "binding", fingerprint(revised))
            c.execute(
                insert(db.events).values(
                    id="enroll_" + member_id(participant),
                    kind="member_enrolled",
                    payload=json.dumps({"member_id": member_id(participant), "can_approve": False}),
                    status="operator_verified",
                    created_at=int(time.time()),
                )
            )
            (ROOT / "config/runtime.pre-enroll.local.json").write_text(original, encoding="utf-8")
            path.write_text(revised.model_dump_json(indent=2), encoding="utf-8")
        print("Enrolled verified member. No spending rights granted. Restart the Coordinator.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
