# Contextual finishing touches

The normal help response exposes three entry points: **what's next**, **show my
trip**, and **checklist**. Other utilities appear only when requested or relevant.
`trip help more` lists advanced shortcuts. No extra agents or services are needed.

## Saved real trips

Next-stop and summary cards use the requesting person's confirmed trip information,
not the simulated booking fixture. Ticket images still use the existing review
flow. A text entry is also available:

```text
trip add Park visit | activity | 2026-11-07T12:00:00+05:30 | 2026-11-07T13:00:00+05:30 | Hyderabad
```

Supported kinds are activity, meal, break, flight, train, hotel, transfer and other.
The response previews the entry. Only its intended person can save it with the
displayed `yes C...` code. Times need an explicit timezone. Imports remain
user-confirmed information, never supplier verification.

| Request | Result |
|---|---|
| `what's next` | Next or in-progress stop, time, map link and compact follow-ups |
| `show route` | Opens a location search in Google Maps; not a verified route |
| `find food` | Existing Planner/place-tool flow using the next location when known |
| `trip travel ID 30` | Saves a user-estimated travel allowance for departure calculation |
| `show my trip` | Bounded summary with stop IDs, transport/stay kinds and map links |
| `checklist` | Tickets, stay details, packing, missing itinerary details, pending reviews |
| `trip done packing` / `trip undo packing` | Reversible self-reported check; also tickets/stay |
| `we're running 20 minutes late` | Suggests shortening/skipping flexible stops; protects fixed ones |
| `trip budget 1500` / `trip budget` | Daily INR budget versus personal shares in sender-recorded bills |
| `why this recommendation` | Saved comparison rationale and remaining evidence gaps |

The current Switch message transport has text and media, not a callback-button
contract. Quick actions therefore use reply shortcuts and clickable map links.
Food searches do not claim restaurants are on the route without route evidence.
No paid restaurant API, live transport provider or booking integration is added.

## Conditional suggestions

- Indoor alternatives are offered only for a stop explicitly marked **outdoor**,
  with a Hyderabad location and fresh, non-demo, date-matched weather. Defaults:
  daily rain probability at least 60%, or a daily high at least 35 C for a stop
  starting between 11:00 and 16:59 IST. These are planning thresholds, not safety
  thresholds or hour-specific forecasts. Unknown/stale weather produces no alert.
- Use `trip mark ID outdoor` or `trip mark ID indoor`; review the confirmation.
  The weather check must have run within 15 minutes (existing weather tools or
  briefing flow). The assistant can also research indoor options on explicit request.
- At most two meal/rest prompts appear in the daily portion of a requested summary.
  They refer to missing *recorded* breaks, not assumptions about what people did.
- Tool facts carry retrieval timestamps and cache labels. Partial failures preserve
  usable results and label unavailable checks. General food preferences appear
  briefly when relevant; personal mobility/health limits are not automatic footers.
- `trip checkin tired`, `busy` or `good` records pace feedback for current/next-day
  planning. It does not automatically rewrite the itinerary or infer health status.

## Edits and notifications

Every stop starts fixed. Transport, hotel and unknown booking kinds remain fixed.
For an optional activity/meal/break, `trip mark ID flexible` previews that setting.
Then `trip skip ID` or `trip move ID ISO_DATETIME` previews an edit. Confirmations
expire after ten minutes and reject wrong senders, replay, stale previews and new
overlaps. Changes affect saved information only, never supplier bookings. Move
preserves duration; travel buffers remain unverified. Skipped stops are omitted
from cards, conversation context and itinerary graphics.

`trip remind 30` sets one next-stop reminder; `trip remind off` disables it.
Reminders are durable and deduplicated, and are discarded once the stop starts or
its saved time changes. `trip checkin 20:30` opts into an evening prompt;
`trip checkin off` opts out. At most one generic evening prompt reaches the group
per day, even when several people subscribe. Quiet hours are 22:00–07:00 IST.
Notifications require the worker to be online; overdue stop reminders are not
replayed after an outage. Prompts omit personal ticket titles and references.

The existing opt-in morning briefing now reports real saved stop counts when
available and points people to their own next-stop cards. Demo data remains labelled.
Budget requests follow the existing explicit group-sharing rule. Budget arithmetic
uses only the sender's unambiguous split share on bills they recorded that local
day; other people's reports, unassigned shares and unrecorded spending are excluded.
The personal checklist, budget setting, notification settings and pace feedback
are removed by the existing forget-memory flow. Confirmed itinerary and expense
audit records retain their existing separate retention behavior.

## Rollout and validation

No database migration is required: utilities use sender-scoped runtime state and
the existing confirmation/import tables. Restart the worker to load backend changes.
Sync the updated `agents/advisor.md` through the existing role-configuration process
to enable the new natural-language assistant actions in a live Switch deployment.
Local verification does not imply that the live agent instructions were synced.

Tests cover weather suppression, confirmation ownership/expiry/concurrency,
fixed-booking protection, reminder dedupe and expiry, quiet hours, budget scopes,
meal/rest prompts, privacy and memory deletion, alongside the existing suite.
