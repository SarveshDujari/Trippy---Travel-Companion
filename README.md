# Trippy — Travel Companion

A family travel assistant that discusses plans in Telegram, checks travel data,
and helps repair a disrupted itinerary through **Switch**. Trippy combines three
separate agent roles with a deterministic Python backend: the model proposes;
application code validates; a linked family member approves reservation changes.

**Status:** working prototype for Hyderabad. Reservation changes are simulated.
Real purchases and payment processing are outside the project. The latest new
Telegram flows still require live verification after reconnecting Switch.

## Features

- **Travel conversation:** sightseeing, food, packing, comfort and itinerary advice.
- **Disruption recovery:** simulated flight-delay events trigger dependency-aware
  replanning, invalidate old proposals and request fresh approval.
- **Scoped decisions:** exact changes and cost deltas with short approval/rejection
  codes, verified sender identity and expiry checks.
- **Family preferences:** reported member needs, evidence messages, importance,
  hard/soft constraints and deterministic per-person comparison scores.
- **Selective tools:** weather, places and walking/driving routes requested on demand.
- **Grounded output:** declared factual claims checked against saved tool results
  with generated source citations.
- **Trip utilities:** comfort scores, expense splits, opt-in briefings, calendar
  export, Hyderabad tips and booking search links.
- **Recovery and tracing:** durable inbox/outbox, replay protection, workflow
  inspection and an actual-process crash-recovery demonstration.

## Architecture

```mermaid
flowchart TD
    Family[Family Telegram group] <--> Switch[Switch bridge]
    Switch <--> Coordinator[Coordinator: deterministic Python runtime]
    Coordinator <--> DB[(SQLite)]
    Coordinator --> Planner[Planner: discussion and proposed changes]
    Planner --> Tools[Bounded read-only tool jobs]
    Tools --> Weather[Open-Meteo]
    Tools --> Maps[Nominatim / Overpass / routing]
    Tools --> DB
    Planner --> Validation[Constraint and evidence validation]
    Validation --> Approval[Human approval with exact scope and expiry]
    Approval --> Reservations[Reservation agent]
    Reservations --> Simulator[Transactional reservation simulator]
    Simulator --> DB
    DB --> Outbox[Durable outgoing messages]
    Outbox --> Switch
    Demo[Authenticated simulated delay webhook] --> Coordinator
```

There are exactly **three roles**:

| Role | Responsibility |
|---|---|
| Coordinator | Authenticates senders, routes messages, validates results, owns state and approvals, and delivers replies. Runs as application code. |
| Planner | Discusses the trip, requests permitted read-only tools and proposes repairs. |
| Reservation | Requests execution of approved simulated changes. Cannot approve spending or claim supplier confirmation. |

Planner and Reservation exchange structured messages with the Coordinator in a
private Switch workroom. The Coordinator sends readable replies to the family
group. Files under `agents/` are functional instructions loaded by the integration.

### Storage and reliability

SQLite stores trip items and dependency edges as relational tables. A recursive
query finds downstream items affected by a delay. Separate tables hold proposals,
actions, preferences, expenses, discussions, tool jobs and processed events.

- Alembic migrations manage schema changes.
- Foreign keys, WAL mode and short transactions protect local state.
- Message/event IDs and unique action keys prevent duplicate local processing.
- Trip versions, assignment IDs and expiry checks reject stale results/approvals.
- Network requests happen outside database transactions.
- Uncertain external delivery is held for review rather than blindly resent.
  Exactly-once Telegram delivery is not claimed.

## Stack

Python 3.13 · SQLAlchemy · SQLite · Alembic · Pydantic · HTTPX · FastAPI · pytest ·
Ruff · Switch · Telegram

Dependencies are pinned in `uv.lock`. The current integration uses managed Codex
roles in Switch; the Coordinator does not call a model-provider API directly.

## Local setup

Requirements: Git, Python **3.13**, and [uv](https://docs.astral.sh/uv/).
Switch/Telegram credentials are unnecessary for isolated tests and the simulator.

```powershell
git clone https://github.com/SarveshDujari/Trippy---Travel-Companion.git
cd Trippy---Travel-Companion
uv sync --locked
uv run python scripts/check_environment.py
Copy-Item config/runtime.example.json config/runtime.local.json
uv run python -m trip_coordinator init
```

`init` applies migrations and creates the fictional Hyderabad fixture. It does
not connect to Telegram or send messages. Replace placeholder identities in the
local config with verified Switch IDs before starting a live worker.
Settings come from that JSON file and explicit environment variables; the app
does not automatically load `.env` files.

### Local demo utilities

```powershell
uv run python -m trip_coordinator compare
uv run python -m trip_coordinator calendar --output data/hyderabad-demo.ics
uv run python -m trip_coordinator inspect
uv run python -m trip_coordinator trace
uv run python scripts/crash_recovery_demo.py
```

The comparison uses fictional candidate metrics. Calendar events are labelled
simulated. The crash demo uses a disposable database, terminates only its own
child process mid-transaction, then verifies rollback and safe replay.

## Connect Switch and Telegram

The adapter was developed against Switch Console 0.40.1 / native core 0.31.0 on
Windows. Setup helpers depend on that installation layout and version-specific
management/identity interfaces; they are not a cross-platform installer.

1. Start your existing local Switch server and connect Telegram through Switch.
   Enter the Telegram bot token privately in Switch, not in this repository.
2. Use a dedicated family test group and link the approver's Telegram account.
3. Register one custom Coordinator identity and managed roles named
   `trip-planner` and `trip-reservations`, using the files in `agents/`.
4. Create a private unbridged workroom for internal assignments. Only the
   Coordinator should publish results to the family group.
5. Fill `config/runtime.local.json` with verified room, bridge and transport
   participant IDs. Enable `conversation_enabled` and supply `work_room_id`.
   Grant approval rights only to intended linked members. Display names are not
   authentication.
6. Set the Coordinator's private key in `TRIP_SWITCH_API_KEY`, then run:

```powershell
uv run python -m trip_coordinator run
```

Alternatively, create an **ignored local file** with this shape:

```json
{
  "coordinator_agent_id": "YOUR_VERIFIED_COORDINATOR_AGENT_ID",
  "coordinator_api_key": "YOUR_PRIVATE_COORDINATOR_KEY"
}
```

```powershell
uv run python -m trip_coordinator run --key-file config/switch-secrets.local.json
```

Included Windows setup helpers:

| Script | Purpose and assumptions |
|---|---|
| `connect_local_switch.py` | Discovers/provisions roles using the local Switch admin configuration. Currently expects room `hydrabad trip` and an existing managed agent `trippy` as controller template. Review these assumptions before `--provision` or `--activate`. |
| `map_local_identities.py` | Read-only native Switch database lookup. Run with Switch's own Python environment containing `asyncpg`; it is not an application dependency. |
| `configure_conversation.py` | Loads role contracts and configures the private workroom after runtime config and verified identity mapping exist. |

Do not run a second Telegram poller alongside Switch. If continuity becomes
blocked, inspect room history, proposals and uncertain deliveries before using
`resume --reviewed`. Resuming invalidates outstanding work and requires fresh
approvals.

## Family interaction

Ordinary travel conversation needs no command:

> Can you check the weather in Hyderabad tomorrow?

Explicit commands handle shared state and decisions:

```text
trip help
trip remember Dad max_walk_m 800 hard
trip remember Mom vegetarian true hard
trip remember Kids required_place Charminar soft
trip compare
trip memory
trip forget preference Dad max_walk_m
trip senior on
trip fatigue 650 29 0 45
trip spent 1200 dinner | Dad,Mom,Me
trip expenses
trip calendar
trip briefing 08:00
trip briefing off
trip links Hyderabad
trip tips
trip status
trip plan repair our delayed arrival
```

Fatigue inputs mean metres walked, Celsius, stairs and travel minutes. They are
reported estimates. Briefing times use **Asia/Kolkata** and require a running
worker. Preference subjects currently use one token such as `Dad`; only the
reporting sender can overwrite their structured preference.

A proposal supplies its own code, such as `approve A1B2C3` or `reject A1B2C3`.
A casual “yes” does not authorize changes. Generic Telegram inline approval
callbacks are not exposed by the installed Switch messaging API, so this project
implements the short-code fallback.

`trip forget` clears the sender's local discussion memory and reported
preferences. Booking/expense records, backups and external chat histories are
separate retention stores and are not deleted by that command.

## Simulated disruption demo

With the worker connected to your intended test group:

```powershell
uv run python -m trip_coordinator delay --event-id demo-delay-001 --minutes 120
```

This queues a labelled simulated alert and fresh repair assignment. Reusing the
event ID does not add the delay twice. A new ID represents another event.
Superseded proposal codes cannot authorize the replacement plan. The simulator
supports explicit late-check-in and room-service alternatives in bounded windows.

For a webhook, start a separate process:

```powershell
uv run python -m trip_coordinator serve-demo
```

The API binds to `127.0.0.1:8765` and creates an ignored token file at
`config/demo-token.local.json`. Send:

```http
POST /disruptions/flight-delay
Authorization: Bearer YOUR_PRIVATE_OPERATOR_TOKEN
Content-Type: application/json

{"event_id":"demo-delay-001","minutes":120}
```

`GET /trace` uses the same authorization. No approval or payment endpoint exists.
Keep this demo service on loopback.

## Verification

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check src tests scripts migrations
uv run python scripts/evaluate.py
uv run python scripts/crash_recovery_demo.py
```

Latest local verification: **103 passing tests**, a **30/30 deterministic workflow
scorecard**, and a passing process-crash exercise. The scorecard uses scripted
inputs and mocked provider failures; it is not a live LLM-quality score. GitHub
Actions runs the tests, style checks and crash exercise independently.

Optional read-only live provider probes:

```powershell
uv run python scripts/check_live_tools.py
```

These make public requests and save results under `data/evaluation/`. The latest
probe obtained place data and a walking route; weather was unavailable and stayed
unknown. Availability can vary.

## Current boundaries

- No real airline-status integration, ticket purchases or supplier reservations.
- Conflict scoring uses demo candidates; automatically constructing two fully
  evidence-backed real itineraries remains future work.
- Fatigue is a planning heuristic, not a medical assessment or an automatically
  verified aggregate of every itinerary leg.
- Field-level checks validate declared tool facts. They cannot prove every
  free-form sentence is true or that the model selected the correct date.
- Raw OSM accessibility, dietary and opening-hour tags may be missing/outdated.
  Missing facts remain unknown; walking failures never become driving estimates.
- Structured memory corrections use commands; conversation does not silently
  rewrite shared constraints.
- Expenses record reported costs and equal splits; no money moves.
- Local briefings require an online worker. Recent Telegram/calendar features
  still need live end-to-end verification.

Nominatim requests are cached and rate limited. Respect its
[usage policy](https://operations.osmfoundation.org/policies/nominatim/)
and [OpenStreetMap attribution](https://www.openstreetmap.org/copyright).
Public APIs are not an unlimited production service guarantee.

## Repository layout

```text
agents/                  Runtime role instructions
config/runtime.example.json
migrations/              Versioned database schema
scripts/                 Integration, evaluation and recovery utilities
src/trip_coordinator/    Coordinator, providers and application logic
tests/                   Workflow and regression tests
.github/workflows/       Continuous integration
```

Credentials, local configs, databases, chat evidence, logs, downloads and private
development planning notes are excluded from this repository.
