# Trip Coordinator

This role is the deterministic custom runtime implemented in `trip_coordinator`.
Register exactly one coordinator identity using the documented Switch custom-agent
registration procedure. Invite it to the test room and run the Python worker with
its private agent API key. Do not also launch a Console/Codex session under this
identity: the coordinator owns the room claim and heartbeat.

The role accepts explicit commands from configured family identities: `trip status`,
`trip plan <request>`, `trip approve <proposal_id>`. It validates planner output,
records human approval, dispatches reservation assignments, and summarizes actual
simulator results. It routes natural conversation to the Planner, validates tool
citations, and owns the preference, expense, briefing and calendar commands.
Short `approve CODE` and `reject CODE` replies retain identity and expiry checks.

There are exactly three roles. This runtime is the Coordinator, not a fourth watcher.
