# Trippy — Travel Companion

A family travel assistant that discusses plans in Telegram, checks travel data,
and helps repair a disrupted itinerary through **Switch**. Trippy combines three
separate agent roles with a deterministic Python backend: the model proposes;
application code validates; a linked family member approves reservation changes.

**Status:** working prototype for Hyderabad. Reservation changes are simulated.
Real purchases and payment processing are outside the project. Live checks have
verified sourced weather/place/walking replies, a simulated disruption, a real
linked Telegram account's short-code approval, and all three simulator outcomes.

## Features

- **Travel conversation:** sightseeing, food, packing, comfort and itinerary advice.
- **Disruption recovery:** simulated flight-delay events trigger dependency-aware
  replanning, invalidate old proposals and request fresh approval.
- **Scoped decisions:** exact changes and cost deltas with short approval/rejection
  codes, verified sender identity and expiry checks.
- **Family preferences:** reported member needs, evidence messages, importance,
  hard/soft constraints and deterministic per-person comparison scores. Natural
  statements produce reviewable suggestions before a constraint is saved.
- **Verified family directory:** stable member IDs, full display names and
  reporter-scoped nicknames. Linked relatives confirm identity associations and
  changes to their own constraints; reported companions remain explicitly unlinked.
- **Image understanding:** the existing Planner reads travel screenshots and
  ticket images through Switch. Extracted itinerary details require confirmation
  and are stored separately from simulated reservation actions.
- **Real outing comparison:** two options built from mapped places and walking
  routes, with evidence, per-person scoring and explicit unknowns.
- **Requested graphics:** ask for a visual plan to receive a locally generated PNG
  comparison or itinerary schematic in Telegram.
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
Image input reuses that same signed-in Planner. Rendering uses Pillow locally.
No separate paid model API account or API key is introduced by these features;
the existing Codex account's usage limits still apply.

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

The CLI `compare` utility is an offline fixture with fictional metrics. Conversational
comparisons use the live `compare_plans` tool. Calendar events are labelled
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
trip family
trip visual
trip approvals
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
worker. Natural preferences and names can be stated directly:

```text
Call me Sarvesh
I prefer vegetarian meals
Dad must walk at most 800 metres
Forget my walking limit
Compare outings from Mecca Masjid, visiting Charminar, with Chowmahalla Palace
as an optional extra stop, on 6 November 2026. Show the plan visually.
```

The bot proposes `yes P...` / `no P...` for preferences and `yes L...` / `no L...`
for name/identity changes. These have a ten-minute expiry and cannot approve
reservations. A linked person's preference requires that person's confirmation.
Names can contain spaces. Nicknames such as Dad are scoped to their reporter;
they do not prove who owns a Telegram account.

Send a PNG, JPEG or WebP travel image (at most two, under 8 MB each). The bot
labels the extraction as unconfirmed and proposes `yes I...` / `no I...` before
saving ticket information. Unclear times, dates or timezones remain unknown.
PDF interpretation is not implemented; send a screenshot instead. Image text
cannot issue approval commands. The native `switch-media://` and Matrix `mxc://`
attachment references are supported; arbitrary remote image URLs are rejected.

To enroll another family member, first link their Telegram account in Switch.
With the Coordinator stopped, run `scripts/enroll_family.py --list` using Switch's
native Python environment, then `--enroll EXACT_VERIFIED_RECORD_ID`. The helper
verifies the transport identity, reviews the binding and grants **no spending
permission**. Restart the Coordinator, then use `trip family` and the confirmed
nickname flow. The helper's read-only discovery was checked against the live setup.

A proposal supplies its own code, such as `approve A1B2C3` or `reject A1B2C3`.
A casual “yes” does not authorize changes. Generic Telegram inline approval
callbacks are not exposed by the installed Switch messaging API, so this project
implements the short-code fallback.
Cards show original and proposed times, each cost delta, an absolute India-time
expiry, and copyable decision commands. Original times are snapshotted when a
proposal is created, so later itinerary updates cannot rewrite its history.

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

Latest local verification: **133 passing tests**, a **30/30 deterministic workflow
scorecard**, and a passing process-crash exercise. The scorecard uses scripted
inputs and mocked provider failures; it is not a live LLM-quality score. GitHub
Actions runs the tests, style checks and crash exercise independently.

Optional read-only live provider probes:

```powershell
uv run python scripts/check_live_tools.py
```

These make public requests and save results under `data/evaluation/`. The latest
standalone probe obtained place data and a walking route; weather was unavailable
then. A subsequent live Telegram check successfully obtained the weather too.
Availability can vary.

Live integration checks on November 5, 2026 also verified:

- Planner-requested weather, place and walking tools with field-level citations.
- Calendar upload through Switch and acknowledged outgoing Telegram relays.
- Authenticated simulated delay webhook and duplicate-event suppression.
- A replacement plan with explicit late-check-in and room-service alternatives.
- Approval code received from the configured, linked Telegram member.
- Three confirmed simulator actions and a final result delivered through Switch.
- Travel-image reading through the same Planner, including a fictional ticket's
  destination and departure time; no separate model API was used.
- A name change proposed in ordinary conversation and confirmed by the actual
  linked account.
- Two sourced outing candidates and an on-request comparison PNG delivered
  through Switch. Overpass was unavailable during this check; bounded geocoding
  of the explicitly named extra attraction plus real walking routes succeeded.

The first weather reply exposed a padded-date validation bug (`06` versus `6`).
The validator now compares numeric values, regression tests cover the formatting
case, and the live retry passed. No real supplier reservation or payment was made.

## Current boundaries

- No real airline-status integration, ticket purchases or supplier reservations.
- The comparison builds bounded outings, not a complete optimized multi-day tour.
  Routed walking excludes unknown movement inside venues. Missing hard-constraint
  evidence blocks feasibility rather than being silently guessed.
- Fatigue is a planning heuristic, not a medical assessment or an automatically
  verified aggregate of every itinerary leg.
- Field-level checks validate declared tool facts. They cannot prove every
  free-form sentence is true or that the model selected the correct date.
- Raw OSM accessibility, dietary and opening-hour tags may be missing/outdated.
  Missing facts remain unknown; walking failures never become driving estimates.
- Natural memory corrections require an exact, actor-scoped confirmation;
  conversation does not silently rewrite shared constraints.
- Image extraction is model interpretation, not supplier verification. Reviewed
  imports are informational records and cannot execute bookings or payments.
- Visuals are schematic plan cards, not geographic maps. A failed comparison
  does not produce an unrelated demo plan as its requested graphic.
- Expenses record reported costs and equal splits; no money moves.
- Local briefings require an online worker. Automated tests cover family utility
  commands; this does not imply every utility has been exercised by a real family.

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
