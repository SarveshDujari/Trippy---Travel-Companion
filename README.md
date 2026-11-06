# Trippy — Travel Companion

A family travel assistant that discusses plans in Telegram, checks travel data,
and helps repair a disrupted itinerary through **Switch**. Trippy combines three
core roles and three optional specialists with a deterministic Python backend:
the model proposes, application code validates, and a linked family member
approves reservation changes.

**Status:** working prototype for Hyderabad. Reservation changes are simulated.
Real purchases and payment processing are outside the project. Live checks have
verified sourced weather/place/walking replies, a simulated disruption, a real
linked Telegram account's short-code approval, and all three simulator outcomes.
The newest contextual trip utilities, memory, privacy, verification and specialist changes are implemented
and tested locally. Their updated live Switch deployment is still pending.

**Local evidence:** 219 passing tests · 115/115 deterministic scenario checks ·
process-crash recovery verified. These counts describe application behavior under
controlled scenarios, not a live-model conversation-quality score.

## Contents

- [Features](#features)
- [Contextual trip assistance](#contextual-trip-assistance)
- [Architecture and agent responsibilities](#architecture)
- [Optional specialist roles](#optional-specialist-roles)
- [Worked use cases](#worked-use-cases)
- [Conversation and orchestration](#conversation-and-orchestration)
- [Memory, identity and corrections](#memory-identity-and-corrections)
- [Validation and trust boundaries](#validation-and-trust-boundaries)
- [Storage and reliability](#storage-and-reliability)
- [Stack](#stack)
- [Local setup](#local-setup)
- [Connect Switch and Telegram](#connect-switch-and-telegram)
- [Family interaction](#family-interaction)
- [Simulated disruption demo](#simulated-disruption-demo)
- [Dashboard](#dashboard)
- [Verification](#verification)
- [Privacy and retention](#privacy-retention-and-transparent-memory)
- [Current boundaries](#current-boundaries)
- [Repository layout](#repository-layout)
- [Roadmap](#roadmap)

## Features

- **Contextual finishing touches:** next-stop and trip summary cards, readiness checks,
  delay previews, daily personal budget, optional reminders/check-ins, and reviewed
  saved-itinerary edits. Indoor alternatives require relevant rain/heat evidence;
  the default help stays compact. See [usage and limits](CONTEXTUAL_TOUCHES.md).
- **Travel conversation:** sightseeing, food, packing, comfort and itinerary advice.
- **Disruption recovery:** simulated flight-delay events trigger dependency-aware
  replanning, invalidate old proposals and request fresh approval.
- **Scoped decisions:** exact changes and cost deltas with short approval/rejection
  codes, verified sender identity and expiry checks.
- **Family preferences:** reported member needs, evidence messages, importance,
  hard/soft constraints and deterministic per-person comparison scores. Clear
  personal soft preferences save with undo; hard limits require confirmation.
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
- **Operator dashboard:** timeline, dependency graph, approval history, evidence,
  verification and delivery views, plus authenticated simulated-demo controls.
- **Selective specialist handoffs:** Researcher gathers evidence, Mediator explains
  computed compromises, and Watcher assesses sourced events. Simple questions
  continue through the direct Planner/tool flow.
- **Recovery and tracing:** durable inbox/outbox, replay protection, workflow
  inspection and an actual-process crash-recovery demonstration.

## Contextual trip assistance

The default help stays small: **what's next**, **show my trip**, and **checklist**.
Other tools appear when requested or relevant, rather than crowding every reply.
These cards use the requesting person's saved, confirmed trip information and
keep it separate from the simulated booking demo.

| Feature | How it works |
|---|---|
| Next stop and quick actions | Shows the next stop, time and map link. Reply `show route`, `find food`, or `remind me`. A saved user-estimated travel allowance enables a departure-time calculation. |
| Trip summary | Shows saved stops, dates, stays, transport and location links. |
| Readiness checklist | Tracks self-reported tickets, stay details and packing; flags missing stop details and pending reviews. |
| Running late | `trip late 20` previews which flexible stops could be shortened or skipped while protecting fixed bookings. |
| Weather alternatives | Offers indoor alternatives for explicitly outdoor Hyderabad stops only with fresh, relevant rain/heat evidence, or when explicitly requested. No default backup for every stop. |
| Meal and rest gaps | A requested summary flags long scheduled stretches without recorded meals or breaks, with at most two prompts. |
| Daily personal budget | Compares a daily INR budget with the person's identifiable split shares in expenses they recorded that day. |
| Recommendation explanations | Explains comparison order, unmet requirements and unknowns; relevant food preferences are visible and correctable. |
| Evidence freshness | Tool facts show check times and cached-result labels. Failed checks are identified while usable results remain available. |
| Optional reminders | One next-stop reminder, durable deduplication, quiet hours and an off control. Passed stops do not generate late catch-up alerts. |
| Evening check-in | Opt-in pace feedback informs current/next-day suggestions without automatically rewriting the plan. |
| Reviewed itinerary edits | Add, mark, move or skip eligible saved stops through an expiring confirmation preview; no supplier booking is changed. |

Examples:

```text
what's next
show my trip
trip done packing
trip late 20
trip remind 30
trip remind off
trip checkin 20:30
trip checkin tired
trip checkin off
Share with group: trip budget 1500
```

Notifications require the worker to remain online. Reminder/check-in quiet hours
are **22:00–07:00 IST**. Quick actions are text shortcuts and clickable map links;
the current Switch transport does not expose native Telegram callback buttons.
Food discovery uses the existing tools; a complete route-aware restaurant itinerary
and live flight/train status integrations are not included in this update.

See [the contextual feature guide](CONTEXTUAL_TOUCHES.md) for weather thresholds,
text itinerary entry, edit confirmations, privacy, budget calculations and rollout.
No database migration is needed. The live worker must be restarted and the updated
agent instructions synced before these local changes are available in Switch.

## Architecture

```mermaid
flowchart TD
    Family[Family Telegram group] <--> Switch[Switch bridge]
    Switch <--> C[Coordinator: routing and durable state]
    C <--> DB[(SQLite)]
    C <--> P[Planner: conversation and proposed changes]
    C <--> R[Researcher: optional evidence gathering]
    C <--> M[Mediator: optional trade-off explanation]
    C <--> W[Watcher: optional event assessment]
    C --> Tools[Validated read-only tool jobs]
    Tools --> Weather[Open-Meteo]
    Tools --> Maps[Nominatim / Overpass / routing]
    Tools --> DB
    Schedule[Scheduled weather observations] --> C
    Demo[Authenticated simulated delay event] --> C
    C --> V[Evidence, constraint and identity validation]
    V --> Approval[Exact human proposal approval]
    Approval --> C
    C <--> Reservation[Reservation role]
    C --> Simulator[Transactional reservation simulator]
    Simulator --> DB
    DB --> Outbox[Durable outgoing messages]
    Outbox --> Switch
```

There are **three core roles**, plus **three optional specialists**:

| Role | Responsibility |
|---|---|
| Coordinator | Authenticates senders, routes messages, validates results, owns state and approvals, and delivers replies. Runs as application code. |
| Planner | Discusses the trip, requests permitted read-only tools and proposes repairs. |
| Reservation | Requests execution of approved simulated changes. Cannot approve spending or claim supplier confirmation. |
| Researcher (optional) | Gathers bounded travel evidence for the Planner using existing read-only tools. |
| Mediator (optional) | Explains trade-offs and selects a feasible, deterministically scored option. |
| Watcher (optional) | Assesses scheduler-generated current-weather events against the simulated itinerary and advises the family. |

The model roles exchange structured messages with the Coordinator in a
private Switch workroom. The Coordinator sends readable replies to the family
group. Files under `agents/` are functional instructions loaded by the integration.

### Why this architecture

The three core roles own the main workflow. Application logic separates `memory.py` (transparent
cards and confirmed utilities), `privacy.py` (audience policy and retention), and
`verifier.py` (forecast scope, provider-content screening and validation entry point).
The Coordinator remains the transactional orchestrator. Authorization and retention
decisions are enforced in application code.

A transport interface and a full FakeTransport smoke test would be useful next
for portability and a no-Switch demo. PostgreSQL, a distributed queue and additional
agents are not required for the current single-worker deployment. They should
follow demonstrated concurrency or deployment needs. Setup helpers still have
version-specific Windows assumptions and some fixed room/template names.

## Optional specialist roles

The specialists have durable assignments and distinct verified Switch identities.
They do not become extra approvers, bypass validation or replace the Coordinator.
When their participant IDs are null, the original core workflow remains available.

```mermaid
flowchart TD
    Family[Family in Telegram] <--> C[Coordinator]
    C <--> P[Planner]
    P -->|Bounded research objective| C
    C <--> R[Researcher]
    R -->|Typed tool request| C
    C --> T[Existing read-only travel tools]
    T --> C
    P -->|Computed options available| C
    C <--> M[Mediator]
    Schedule[Scheduled weather checks] --> Change[Meaningful sourced event]
    Change --> C
    C <--> W[Trip Watcher]
    C -->|Validated evidence advisory| Family
    C --> Approval[Human proposal approval]
    Approval --> Reservation[Reservation role and simulator]
```

### Researcher

The Planner delegates an evidence-gathering objective through `trip_delegate`.
The Researcher may request the same bounded tools as the Planner; it shares the
three-tool limit across the entire discussion rather than receiving a new budget.
Each tool continuation gets a fresh assignment ID. A validated
`trip_specialist_result` stores advice and verified citations, then resumes the
Planner. The Researcher cannot mutate memory, approve, book, delegate again or
publish directly to the group.

### Family Mediator

Delegation requires existing computed plan options. The Mediator explains the
trade-offs using supplied preferences and evidence, without recalculating scores
or inventing plans. The Coordinator rejects a recommendation blocked by hard
constraints. If neither option is feasible the Mediator may return no selection;
it cannot manufacture an acceptable compromise. Private personal thresholds remain
withheld under group privacy. Recommendations are advice, not family authorization.

### Trip Watcher

Current-weather observation runs in application code when `weather_enabled=true`.
A potentially relevant rain observation can queue a Watcher assignment. Event IDs
are deduplicated, a one-hour event cooldown applies, and default quiet hours are
22:00–07:00 Asia/Kolkata. Equal start/end hours disable the quiet window.
Events must be recent, and the simulated itinerary must contain items on the
observation date. Quiet hours are checked again before alert delivery.

The Watcher returns sourced claims, eligible itinerary item IDs and whether
alternatives may be worth discussing. The Coordinator validates these and renders
the group advisory from evidence, not raw model prose. It makes no booking change
and does not automatically authorize a repair. The family can ask the Planner to
prepare alternatives. This integration currently covers **current weather and the
simulated itinerary**; real flight-status monitoring, forecast heat triggers and
full real-itinerary monitoring remain future extensions.

### Bounded orchestration and recovery

A discussion records `assigned_role` and `specialist_notes` in SQLite (migration
`0005`). Completed specialist notes carry their status, advice, verified citations
and any recommendation back to the Planner. Only the authenticated owner of the active assignment can respond.
Researcher/Mediator may each be delegated once per discussion; they return to the
Planner rather than starting agent-to-agent loops. Mediator and Watcher cannot
request tools. Private-room checks, deadlines, shared tool budgets, validation,
privacy and durable delivery remain application-owned.

These are serial, optional handoffs. Simple conversations do not need extra model
calls. More complex requests may take longer and consume more of the existing
signed-in model account's usage allowance. No separate model API client or paid
API account is introduced.

### Connecting the specialists

The Switch server must be running. Stop the Coordinator and back up its SQLite
database before applying migrations. Then:

1. Run `uv run python scripts/connect_local_switch.py --provision --specialists`
   to provision the additional managed roles in a stopped state.
2. Run the existing identity-mapping helper with Switch's native Python environment
   to map actual verified transport identities. Never derive IDs from names.
3. Run `uv run python scripts/configure_conversation.py` to load specialist
   instructions and join all configured roles to the private workroom. The helper
   restricts their addressing policy to the Coordinator and starts managed roles.
4. Run `uv run python -m trip_coordinator init` to apply migration `0005` without
   resetting an existing itinerary. Finish/cancel active work before changing role
   bindings; identity/room changes retain the existing migration safeguards.
5. Start the Coordinator normally. Enable `weather_enabled` only if weather polling
   is desired. Optional participant fields are `researcher_participant_id`,
   `mediator_participant_id` and `watcher_participant_id`; null disables a role.

The dashboard lists configured specialists. Local tests verify handoffs and
boundaries using protocol fixtures; they do not establish live specialist model
quality. Provisioning and live checks remain pending until Switch is connected.

## Conversation and orchestration

Users speak to one assistant. They do not have to choose an agent or know a tool
name. The Planner interprets a request; the Coordinator controls the resulting
workflow. Agent responses are typed data, never independent authorization.

```mermaid
sequenceDiagram
    participant F as Family member
    participant S as Switch
    participant C as Coordinator
    participant P as Planner
    participant T as Travel tools
    participant D as SQLite
    F->>S: Compare two outings and show a visual
    S->>C: Verified transport event
    C->>D: Deduplicate and queue discussion
    C->>P: Private assignment with bounded context
    P->>C: Typed compare_plans request
    C->>T: Execute validated read-only request
    T-->>C: Evidence or explicit unavailable result
    C->>D: Persist tool evidence
    C->>P: Continue assignment with evidence
    P->>C: Typed reply, claims and visual request
    C->>C: Validate assignment, evidence and output
    C->>D: Commit reply and media delivery intent
    C->>S: Deliver readable reply and PNG
    S-->>F: Comparison and plan schematic
```

### Context and automatic routing

Each assignment includes the current request, sender, request time, family
directory, relevant preferences, imported itinerary, pending demo approvals and
bounded conversation history. History carries speaker, message and discussion
references. The history window is limited to 12 chat entries and 10,000 characters;
it is not unlimited recall of the Telegram group.

The Planner can select weather, places, route or compare_plans through a typed
request. The Coordinator validates arguments, runs the tool outside a database
transaction, saves its evidence and resumes the assignment. Tool requests are
bounded to three per discussion. Missing essential inputs should produce a short
clarifying question. Speaker labels help interpretation; they do not guarantee
that the model resolves every ambiguous follow-up correctly.

| Natural request | Application behavior |
|---|---|
| “What should we carry tomorrow?” | Discuss packing; request relevant weather when needed |
| “Compare Charminar outings from our starting place” | Ask for missing origin/date, then use compare_plans |
| “Show the plan as a timeline” | Generate a local PNG from saved plan data |
| “Who is in the family?” | Route to the family directory |
| “Show pending changes” | Render stored approval cards |
| “Export our itinerary to a calendar” | Produce the labelled simulated calendar |
| “Prepare changes to our delayed arrival” | Start proposal planning for the simulated itinerary |
| “Yes, go ahead” | Discuss the request; does not authorize reservations |

Optional `trip ...` shortcuts remain available for explicit operation and testing.
The `assistant_action` field supports family, approvals, calendar, status,
propose_changes, memory, privacy, expenses and confirmed memory deletion. The visual request has a separate field. Real outing comparisons
remain separate from the fictional reservation fixture.

### Conversation lifecycle

A message becomes a durable discussion, then a private Planner assignment.
A tool request temporarily moves it into tool processing before continuation.
Completed replies enter the outbox. Assignment IDs and deadlines reject responses
from earlier work. One bounded schema-repair attempt is available for malformed
structured replies; factual validation failures cannot be repaired by bypassing
the evidence checks. Rate limits bound per-sender and total queued discussion work.

## Memory, identity and corrections

Memory has several distinct purposes. Keeping them separate prevents a nickname,
a reported wish or an image interpretation from becoming booking authority.

| Memory | Stored information | How it changes |
|---|---|---|
| Conversation history | Speaker, message reference, request, assistant response | Recorded as discussions progress; bounded when passed to the model |
| Explicit trip details | Destination, dates, budget, food, mobility and other typed facts | Require evidence from the current request |
| Family directory | Stable member ID, linked participant ID, display name | Verified enrollment; own display name can change immediately |
| Preferences | Owner, kind, value, hard/soft flag, strength, author and evidence reference | Immediate reversible update for own soft preferences; confirmation otherwise |
| Imported itinerary | Reviewed image-derived flight/hotel/activity information | Uploader confirms; stored as informational rather than supplier-verified |
| Workflow history | Proposals, decisions, actions and events | Application-controlled audit and recovery state |

### Stable identities and reported companions

Authentication uses configured Switch transport identities, not names. Stable
member IDs derive from those identities. “Call me Sarvesh” changes the sender's
own display name immediately, even if a model supplies another member ID.
It does not grant approval rights or change the account's identity.

Aliases such as Dad are scoped to the person reporting them. Two members can
refer to different people as Dad. A reported companion stays unlinked until the
user chooses an existing verified member and that member confirms the link.
Reported preferences transfer only with that consent; conflicts or stale state
require a fresh request. New enrollment grants no spending rights by default.

### Preference updates and undo

The Planner proposes typed preferences with an exact quote from the current
message. Questions and hypotheticals cannot become preferences; numeric limits
must be supported by the quoted value, including supported kilometre conversion.

- “I prefer vegetarian meals” can save immediately as the sender's soft preference.
- “I must walk at most 800 metres” requires confirmation as a hard constraint.
- A linked relative must confirm changes to their own preferences.
- Unlinked companions' needs remain explicitly reported by their author.
- “Forget my walking limit” proposes removal of the existing typed preference.

Conflicting replacements show the existing value and evidence beside the proposed
value and new evidence, and require confirmation even when the new preference is
soft. Preference-change events retain before/after history, including removals.

An immediate update provides `undo Pxxxxxx`, valid for ten minutes. Undo is tied
to the original sender, works once and checks that the preference has not changed
again. It restores the previous record rather than overwriting a newer correction.
Hard-constraint confirmations have similar ownership, expiry and stale-state checks.

`trip forget` clears the sender's discussion memory and reported preferences.
It is not a complete erasure of expense/booking audits, backups or external chats.

## Validation and trust boundaries

Validation is application code. Prompt instructions guide the model, while
Pydantic contracts and stored workflow state enforce accepted inputs and actions.

| Boundary | Checks |
|---|---|
| Incoming events | Configured room/bridge and participant identity, duplicate event handling, freshness for human decisions |
| Agent handoffs | Expected agent, private workroom, active assignment, task/discussion identity and deadline |
| Structured output | Typed fields, permitted operations, bounded lists/text and rejection of extra fields |
| Tool access | Allowlisted tools and argument schemas; bounded requests rather than arbitrary model URLs or code |
| Factual claims | Available evidence ID, JSON pointer, exact supported scalar value and generated citation |
| Preference memory | Current-message evidence, valid values, ownership, confirmation or reversible own-profile update |
| Image imports | Valid attached image, typed extraction, explicit review before persistence |
| Plan proposals | Current trip version, dependency/time/budget constraints and permitted alternatives |
| Reservation execution | Authorized human approval, exact proposal, expiry and replay checks |

### Grounded answers

Tool results have source URLs, retrieval timestamps and a status. A declared
claim identifies an evidence source and JSON pointer such as
`/plans/0/walking_m`. The Coordinator compares the value with the stored evidence
and renders the factual response directly from checked claims. Model reply prose
is omitted from that factual response; optional suggestions are labelled as advice.
Named places present in tool results also require an explicit claim when repeated.
The named Verifier checks forecast dates and screens instruction-like provider
content, with successful verification checks recorded for the dashboard. Unknown values and compound objects cannot be cited as
verified scalar facts. Numeric formatting such as `06` versus `6`, or `1,671`
versus `1671`, is normalized where relevant.

These checks validate structured facts and restrict unsupported numerical output.
They do not constitute exhaustive natural-language fact checking: general advice
and place names absent from the evidence still need live conversation evaluations.
Supported forecast-date checks include today/tomorrow, day after tomorrow, weekdays,
ISO dates, day-month dates and bounded explicit date ranges. Ambiguous references
to trip dates or a weekend require clarification; arbitrary temporal language is
not fully parsed. Current conditions cannot answer a future-date forecast.

### Provider behavior and comparisons

- Weather uses Open-Meteo for Hyderabad; location support is deliberately bounded.
- Places use Nominatim and optional Overpass enrichment. Missing tags stay unknown.
- Walking uses a walking router; driving results never substitute for walking.
- Place/route GET requests retry at most once for transient transport or
  502/503/504 failures. Rate-limit responses are not automatically retried.
- Tool caches retain retrieval timestamps. Cache hits include age and expiry.
  Weather normally has a 15-minute cache; successful place/route results use a
  day; failed or incomplete results have a short retry window.
- If nearby enrichment fails, comparison can use bounded geocoding of an explicitly
  named extra attraction or a bounded museum search. Usable route evidence is
  still required; distance is never fabricated.

A comparison constructs two bounded outings, not an optimized multi-day tour.
Per-person scores reflect weighted preferences. Violated hard requirements block
feasibility. Unknown accessibility, stairs or venue walking are surfaced; a short
mapped route alone cannot prove a hard total walking limit.

The fatigue score is a transparent planning heuristic using walking, heat, stairs
and travel time. Output includes known inputs, missing inputs and whether venue
walking is excluded. Missing inputs do not become zero or a fabricated score.

### Image pipeline

Incoming PNG/JPEG/WebP attachments are validated, decoded and re-encoded before
forwarding to the Planner's private assignment. Limits are two images, 8 MB per
image and 20 megapixels; corrupt or animated images are rejected. Re-encoding
strips image metadata. Supported transport references are `switch-media://` and
`mxc://`; arbitrary HTTP/data URLs are not accepted as attachment references.

The signed-in Planner reads the image through Switch's attachment support. Image
text is data, never instructions or approval authority. Missing years/timezones
remain unknown. Extracted itinerary entries require an uploader-scoped `I...`
confirmation and retain an informational, not supplier-verified, status.

## Storage and reliability

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

### Data model

| Table group | Purpose |
|---|---|
| Trips, itinerary items, dependencies | Current fixture state and the directed relationships used for disruption propagation |
| Tasks, proposals, reservation actions | Planning/execution state, exact approved scope and idempotent local actions |
| Trip events | Disruption IDs and original-time snapshots for historical proposal cards |
| Family members and aliases | Verified identity mapping and reporter-scoped nicknames |
| Preferences and explicit trip facts | Typed memory with owner and evidence references |
| Confirmations | Actor-scoped, expiring preference, identity and image-import decisions |
| Imported itinerary | User-reviewed extracted information, separate from simulator bookings |
| Discussions, chat and tool jobs | Durable conversation state, bounded context and recorded evidence |
| Plan comparisons | Saved computed options and their evidence/provenance |
| Inbox, outbox and runtime state | Deduplication, delivery intent, continuity, caches and reversible-memory snapshots |
| Expenses and weather observations | Reported costs, splits and briefing/observation bookkeeping |

The dependency graph is stored as relational edges, rather than requiring a graph
database. For example, a delayed flight can affect the transfer, then hotel
check-in, then dinner. Recursive traversal finds the affected descendants.
SQLite fits a single locally running Coordinator and keeps deployment simple.
A server database becomes relevant for multiple concurrent worker instances,
remote access or larger operational requirements.

### Reservation lifecycle

```mermaid
stateDiagram-v2
    [*] --> planning
    planning --> awaiting_approval: Validated proposal
    awaiting_approval --> executing: Authorized exact-code approval
    awaiting_approval --> cancelled: Rejection
    executing --> completed: All simulated actions succeed
    executing --> partially_completed: Some actions fail
    planning --> failed: Deadline or invalid workflow
    awaiting_approval --> failed: Expiry
    executing --> failed: Deadline
```

The approval card shows original and proposed times, per-item and total cost
deltas, status and an absolute expiry in India time. Original times are
snapshotted when the proposal is created. New disruptions invalidate old work;
a fresh plan requires fresh approval. Successful partial outcomes remain recorded
and are not silently rolled back or repeated.

This simulator executes inside a local transaction. A real supplier integration
would need separate durable action intents, provider idempotency and reconciliation
for uncertain external outcomes. A local database transaction cannot make an
external payment or reservation exactly-once.

## Worked use cases

The following are illustrative request flows, not transcripts of live tests.
Provider results and wording vary. The roles choose bounded work; users do not
need to type agent names or coordinate them manually.

### 1. A quick weather question

> “What is the weather in Hyderabad tomorrow? What should we carry?”

The Planner requests weather through the Coordinator. The response cites the
forecast row for tomorrow, resolved against the request time in Asia/Kolkata.
Application validation rejects a citation for today even if its temperature
happens to match tomorrow's. Checked weather values are rendered as sourced facts;
packing suggestions are labelled as advice. No Researcher or Mediator is needed.
If the provider is unavailable, the assistant must say so rather than invent rain.

### 2. Researching two outings and explaining a compromise

> “Compare a short outing around Charminar with a longer option including
> Chowmahalla Palace, starting from Mecca Masjid. Show the plan visually.”

The Planner asks for a date if one is missing. It may delegate a focused evidence
question to the Researcher, or request compare_plans directly. Application tools
resolve places, obtain walking routes and compute two options. The Mediator can
then explain a compromise and recommend an existing feasible option. It cannot
create new scores or override a hard requirement. The Planner receives the notes
and the Coordinator delivers the checked comparison and local PNG schematic.

If venue walking or wheelchair access is unknown, that remains visible. A shorter
mapped route cannot prove the family's full walking requirement is satisfied.
When group privacy applies, scoring still respects private hard constraints while
personal thresholds, per-person scores and detailed blocker labels are withheld.

### 3. A name, a preference and a correction

> “Call me Sarvesh.”
>
> “I prefer vegetarian meals.”
>
> “Actually, I don't prefer vegetarian meals anymore.”

The name changes immediately for that verified sender. A clear own-profile soft
preference saves with a ten-minute undo code. A conflicting correction shows the
old value/evidence beside the new value/evidence and asks for confirmation.
A changed preference cannot be silently overwritten by an older undo operation.
Another linked family member must confirm changes to their own preferences.

“What do you remember about my family?” returns a readable memory card rather
than raw database JSON. Sensitive entries are hidden under the group policy.

### 4. Recording a dinner expense

> “Share with group: Spent 1200 on dinner among Dad, Mom, Me.”

The Planner extracts an expense draft with the explicit amount, description,
participants and exact source quote. The Coordinator validates the values and
shows an E-prefixed confirmation. The reporting sender confirms before a single
expense record is written; repeated confirmation cannot create a duplicate.
This is equal-split bookkeeping, not a payment or debt collection system.
Missing participants trigger clarification rather than a guessed family list.

Without the sharing prefix, sensitive expense requests are withheld with an
explanation. “Show my expenses” is scoped to the reporting payer's records.

### 5. Understanding a travel ticket screenshot

A family member sends a readable PNG/JPEG/WebP screenshot. The application validates
and re-encodes the attachment, then forwards it to the signed-in Planner. The
Planner reports visible text and uncertainties. Missing years or timezones are
not guessed. A reviewed I-prefixed confirmation is required before saving the
draft itinerary, whose status remains informational rather than supplier-verified.
Instructions embedded in a screenshot cannot approve changes or issue commands.

### 6. Recovering from a simulated flight delay

The operator triggers a stable simulated delay event. The dependency graph identifies
items downstream of the flight, a new planning task begins, and older proposals
are invalidated. A validated repair card shows exact time changes, extra costs,
alternatives and expiry. An authorized linked family member sends its exact code.
The Reservation role requests execution and the Coordinator applies simulator
actions transactionally. Partial failures retain successful results and require
new planning and approval for further changes. No real supplier is contacted.

### 7. Proactive weather review

With the Watcher configured and polling enabled, a fresh relevant rain observation
on the simulated itinerary's date can queue an assessment. The application checks
quiet hours, event deduplication and cooldown before spending a model call. The
Watcher identifies eligible items worth reviewing; the Coordinator publishes only
validated evidence and the review list. A weather observation does not prove that
an activity is cancelled, and it does not authorize reservation changes.

### 8. Provider outage during a presentation

The operator explicitly enables cached-demo mode in the dashboard. Tool requests
use successful historical entries at most seven days old, with age and demo labels.
Missing cache entries remain unavailable. The mode does not fabricate a replacement
route, call providers in the background or present historical weather as current.
Switch and the model connection are still needed for a live conversation; the
CLI fixture and isolated tests are the no-Switch local demonstration path.

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

Your own display name updates immediately, without a confirmation code. Clear soft
preferences for your own profile also save immediately, with a sender-scoped undo
code valid for ten minutes. Hard limits and another linked person's preferences
still require confirmation. Undo refuses to overwrite a newer correction. Normal
conversation selects tools and agent handoffs automatically: ask for weather,
family members, the saved itinerary, a calendar, pending proposals or proposed
changes without commands. Reservation execution still requires explicit approval.

The bot proposes `yes P...` / `no P...` for preferences and `yes L...` / `no L...`
for linking identities. These have a ten-minute expiry and cannot approve
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

## Dashboard

Run `python -m trip_coordinator serve-demo` and open
`http://127.0.0.1:8765/dashboard`. Enter the private token from the ignored
`config/demo-token.local.json` file. The dashboard shows itinerary dependencies,
agent responsibilities, proposal states, tool activity and delivery status.
Its data endpoints require authentication; it cannot approve proposals or execute actions.
The token stays in the page input and is not saved in browser storage or URLs.

The dashboard now provides a chronological timeline, an SVG dependency graph,
paginated approval history, an evidence browser with source links and a Verifier
check log. Operator controls can simulate a delay or toggle cached-demo mode.
Neither control approves a proposal or executes a booking. The delay button calls
the existing authenticated, idempotent endpoint; actual reservation authorization
still comes from the configured Telegram approver.

`POST /demo-mode` with `{"enabled":true}` enables historical-cache-only tool use.
Successful cached evidence must be at most seven days old; missing entries return
unavailable without contacting providers. Replies, comparison cards and generated
visuals label cached-demo evidence as historical rather than live. Disable the
mode with `{"enabled":false}`. Normal live mode does not silently use stale data.
This is not an automatically prepopulated offline dataset.

Run `python scripts/evaluate.py` for a reproducible scenario scorecard in
`data/evaluation/scorecard.json`. This measures deterministic workflows and mocked
provider boundaries; it does not claim live language-model conversation accuracy.
Conversation context includes speaker and message references to support follow-ups.

Place and route lookups retry one transient transport/502/503/504 failure. Rate
limits are not retried. Cached results retain their original retrieval timestamp
and include cache age/expiry. Existing evidence-only fallback comparison remains.
Fatigue output lists known and missing inputs and flags excluded venue walking;
missing inputs never silently become zero.

The dashboard shell is available at `/dashboard`; it contains no trip data.
`GET /dashboard/state` requires a bearer token, as do `/trace` and the simulated
delay endpoint. The dashboard renders server values as text, keeps the token in
the page input and exposes no approval or payment action. Refresh is manual.
The local demo server must be running; starting it does not start the Coordinator.
The trip views are read-only. Only the clearly labelled simulated controls write
demo workflow state.

## Verification

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check src tests scripts migrations
uv run python scripts/evaluate.py
uv run python scripts/crash_recovery_demo.py
```

Latest local verification (November 6, 2026): **193 passing tests**, a
**115/115 deterministic scenario scorecard**, and a passing process-crash exercise. The scorecard uses scripted
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

Earlier live integration checks verified:

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

The most recent memory routing, privacy/retention, forecast-date verification,
approval throttling and dashboard changes have passed local tests. The dashboard
was also checked in a browser using isolated fictional data. Their updated Switch deployment and
live Telegram checks remain pending because the local Switch API was unavailable
during the latest verification. Multi-person enrollment writes have not been
live-tested with several real relatives.

The first weather reply exposed a padded-date validation bug (`06` versus `6`).
The validator now compares numeric values, regression tests cover the formatting
case, and the live retry passed. No real supplier reservation or payment was made.

## Privacy, retention and transparent memory

`trip privacy` (or a natural privacy question) explains the local policy.
`trip memory` or a natural memory question produces a readable card with the saved
value, owner/reporter, timestamp and evidence message reference. It shows the
sender's explicit facts and shareable family preferences, with personal mobility
and health entries withheld by default.

Runtime settings:

```json
{
  "discussion_retention_days": 30,
  "group_privacy": true
}
```

These keys are optional and the values above are defaults. Completed, cancelled,
failed or expired conversations older than the retention window are scrubbed in
worker maintenance: chat entries, tool jobs and comparisons are removed, outbox
text/media metadata is redacted, and a minimal expired discussion marker remains.
Active assignments are not deleted mid-flight. Maintenance needs the worker to
run; offline copies are not independently scheduled for erasure.

Saved explicit facts, preferences, reviewed imports, decision audits and reported
expenses have separate retention. This feature does not delete downloaded image
files, generated PNG files, backups, Switch/Telegram history or provider copies.
SQLite record redaction is not a forensic secure-erase guarantee.

With group privacy enabled, personal health/mobility and expense requests require
an explicit `Share with group:` prefix before processing. The bot explains this
without repeating the details. Private-message delivery is not configured. A
person's original group message may already be visible to everyone in Telegram.
Sensitive saved preferences are excluded from ordinary Planner context; pending
confirmation payloads and imported ticket references are withheld. Comparison
scoring still respects private hard constraints while hiding personal scores and
specific blocker details in group output. Sensitivity detection combines typed
preference categories with keyword checks; it is not comprehensive medical-data
classification or a full data-loss-prevention system.

Examples:

```text
What do you remember about my family?
What is your privacy policy?
Share with group: Spent 1200 on dinner among Dad, Mom, Me
Share with group: Show my expenses
Forget my memory
```

Expense capture requires an explicit amount, description and split participants
from the current message. A sender-scoped `E...` confirmation records the expense;
no payment is made. Expense inspection is restricted to the reporting payer's
records. Natural memory deletion uses an `F...` confirmation; `trip forget` remains
an explicit operator-style shortcut. Decision attempts are limited to five per
member per rolling minute, including failed guesses; replayed event IDs do not
consume additional attempts.

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
- Clear own-profile soft preferences save with sender-scoped undo. Hard limits,
  identity links and changes for another linked person require confirmation.
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

## Roadmap

The next priorities build on the core roles and optional specialists:

1. Activate the newest instructions and worker code in a connected Switch session;
   verify instant names, soft-memory undo and automatic handoffs in Telegram.
2. Evaluate realistic multi-person conversations, Hinglish, ambiguous references,
   quoted preferences and blurry images against expected behavior. The current
   deterministic scorecard does not substitute for this live model evaluation.
3. Expand provider fallback coverage and freshness policies while retaining
   evidence provenance and explicit unknowns.
4. Add verified venue walking, rest opportunities and opening-hour interpretation
   before claiming complete comfort-aware day planning.
5. Extend dashboard filtering, operational metrics and evaluation trends.
6. Add real flight-status events and supplier handoffs only with reconciliation,
   provider idempotency and clearly scoped user authorization.

Optional specialists are described in the architecture section. More roles should be added only for an
independently useful responsibility and a testable contract.
