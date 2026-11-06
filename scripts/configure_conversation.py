"""Configure core roles and any explicitly mapped optional specialists."""

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    local = Path(os.environ["APPDATA"]) / "switchdash/local-switch-server"
    env = dict(
        x.split("=", 1)
        for x in (local / "native/run/switch-core.env").read_text().splitlines()
        if "=" in x and not x.startswith("#")
    )
    config_path = ROOT / "config/runtime.local.json"
    config = json.loads(config_path.read_text())
    identity = json.loads((ROOT / "config/coordinator-identity.local.json").read_text())
    with httpx.Client(base_url=config["bridge_url"], timeout=20, trust_env=False) as client:
        response = client.post(
            "/gateway/auth/login",
            json={"email": env["GATEWAY_ADMIN_EMAIL"], "password": env["GATEWAY_ADMIN_PASSWORD"]},
        )
        response.raise_for_status()
        agents = client.get("/gateway/agents").raise_for_status().json()
        wanted = {"trip-coordinator", "trip-planner", "trip-reservations"}
        wanted |= {
            "trip-" + role
            for role in ["researcher", "mediator", "watcher"]
            if config.get(role + "_participant_id")
        }
        roles = {a["name"]: a["id"] for a in agents if a["name"] in wanted}
        if set(roles) != wanted:
            raise SystemExit("Provision every configured role before configuring handoffs")
        rooms = client.get("/gateway/rooms").raise_for_status().json()
        matches = [r for r in rooms if r["name"] == "Trippy private workroom"]
        if len(matches) > 1:
            raise SystemExit("Multiple workrooms found; review configuration")
        if matches:
            workroom = client.get(f"/gateway/rooms/{matches[0]['id']}").raise_for_status().json()
            if workroom["bridge_id"] is not None:
                raise SystemExit("Workroom must have no external bridge")
            missing = [r for r in roles.values() if r not in workroom["agent_ids"]]
            if missing:
                client.post(
                    f"/gateway/rooms/{workroom['id']}/agents", json={"agent_ids": missing}
                ).raise_for_status()
        else:
            workroom = (
                client.post(
                    "/gateway/rooms",
                    json={
                        "name": "Trippy private workroom",
                        "description": "Private agent handoffs",
                        "internal_only": True,
                        "channel_type": "channel_private",
                        "read_visibility": "private",
                        "write_visibility": "private",
                        "agent_ids": list(roles.values()),
                    },
                )
                .raise_for_status()
                .json()
            )
        for name in sorted(wanted - {"trip-coordinator"}):
            client.put(
                f"/gateway/agents/{roles[name]}/addressing-policy",
                json={
                    "policy": {
                        "rules": [
                            {
                                "rooms": [workroom["id"]],
                                "users": [],
                                "agents": [roles["trip-coordinator"]],
                            }
                        ]
                    }
                },
            ).raise_for_status()
            filenames = (
                ["advisor.md", "planner.md"]
                if name == "trip-planner"
                else ["reservations.md" if name == "trip-reservations" else name[5:] + ".md"]
            )
            instructions = "\n\n".join(
                (ROOT / "agents" / file).read_text(encoding="utf-8") for file in filenames
            )
            instructions += (
                "\nVerified Coordinator transport sender: "
                + identity["participant_id"]
                + ". Coordinator Switch agent ID: "
                + roles["trip-coordinator"]
                + ". Private workroom ID: "
                + workroom["id"]
                + ".\n"
            )
            managed = (
                client.get(f"/gateway/management/agents/{roles[name]}").raise_for_status().json()
            )
            definition = managed["definition"]
            definition["instructions"] = instructions
            client.patch(
                f"/gateway/management/agents/{roles[name]}",
                json={"definition": definition, "desired_state": "running"},
            ).raise_for_status()
        config["work_room_id"] = workroom["id"]
        config["conversation_enabled"] = True
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print(f"Configured {len(roles)} roles with private handoffs and conversational mode.")


if __name__ == "__main__":
    main()
