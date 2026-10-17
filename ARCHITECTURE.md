# Daedalus — Architecture

**Companion to:** `SPEC.md` (the requirements/design source of truth) and `AGENTS.md` (working rules).
**Scope:** how the system is built, from the loop outward. Pure architecture — no product narrative.

---

## 1. Overview

Daedalus is a single-user, local-first agent that runs as a **lightweight always-connected daemon** on the owner's laptop and is reached over Discord. It has four architectural commitments that shape everything below:

1. **One loop, many identities.** A single turn engine serves every surface and every cabinet seat. Seats are *identity + charter + memory namespace*, not processes.
2. **Capability, not flow.** The model composes behaviour at runtime and *authors its own tools*; deterministic code owns only hard boundaries.
3. **Structure remote, values local.** Sensitive values never enter a remote prompt; only structure/schema crosses the wire.
4. **File-first state.** Memory, capabilities, tokens, and grants are human-readable on disk, indexed by SQLite.

---

## 2. Architectural principles

Derived from `SPEC.md` §1 and binding on every component:

- **P0 — Free text in, capability not flow, no hardcoded behaviours.** No component ships a fixed behaviour the model cannot compose around.
- **P1 — The model proposes, a deterministic engine disposes.** LLMs decide *what*; deterministic code decides *whether it is allowed*.
- **P2 — Local-first.** Default is on-machine. Egress is a gated exception.
- **P3 — Depth for one person.** Single owner, single trust root.
- **P4 — Bias to action.** The system drives toward the next concrete action and retires stale work.

---

## 3. System context

```
                       ┌───────────────────────────────────────────────┐
   Owner (phone/laptop)│                  LAPTOP (delphi-1)             │
        │              │                                               │
        │  Discord DM  │   ┌──────────────┐      ┌──────────────────┐   │
        ├─────────────►│   │   Surface     │      │   Scheduler      │   │
        │              │   │  (Discord)    │      │  (cron-pumped)   │   │
        │              │   └──────┬───────┘      └────────┬─────────┘   │
        │              │          │  events               │ wakes       │
        │              │          ▼                       ▼             │
        │              │   ┌──────────────────────────────────────┐     │
        │  tailnet     │   │            TURN ENGINE (loop)        │     │
        │  (Serve)     │   │  context ▸ model ▸ tools ▸ output     │     │
        ├─────────────►│   └───┬───────┬────────┬────────┬────────┘     │
        │  dashboard   │       │       │        │        │              │
        │              │       ▼       ▼        ▼        ▼              │
        │              │  ┌────────┐┌────────┐┌────────┐┌─────────────┐ │
        │              │  │ Model  ││ Tools  ││Approval││ Capability  │ │
        │              │  │ client ││(prims) ││middle- ││ substrate   │ │
        │              │  │        ││        ││ware    ││ (author/run)│ │
        │              │  └───┬────┘└────────┘└────────┘└──────┬──────┘ │
        │              │      │            │                  │        │
        │              │      ▼            ▼                  ▼        │
        │              │  ┌──────────────────────────────────────────┐ │
        │              │  │   Memory · Grants · Registry · Secrets   │ │
        │              │  └──────────────────────────────────────────┘ │
        │              └───────────────────────────────────────────────┘
        │                                      │
        │                                      │ (egress, approval-gated)
        ▼                                      ▼
   Phone (Taildrop/Tailscale)          NIM · Z.ai · local runner
```

**Trust boundary:** the laptop. Everything inside is `trusted_owner` or `local_derived`; anything crossing outward is `untrusted_source` on return and is quarantined until re-derived.

---

## 4. Component map

| Component | Responsibility | Key interface |
| --- | --- | --- |
| **Surface (Discord)** | Receive DMs/threads, stream replies, surface approvals as buttons | `Surface` port: `recv() → event`, `send(msg)`, `present(approval)` |
| **Scheduler** | Trigger wakes and run authored capabilities on a period | in-process ticker → `dispatch_tick()`; `agent_wakes` table |
| **Turn engine** | Assemble context, drive model↔tool loop, emit output | `run_turn(event) → TurnResult` |
| **Model client** | One OpenAI-compatible client, provider chain, local fallback | `chat(msgs, tools) → stream` |
| **Rate-limit ledger** | Cross-process rolling-window accounting | SQLite `ratelimit.db` |
| **Tools (primitives)** | The 13 Layer-1 primitives + the `capability` tool | `Tool` protocol (name · schema · risk · `call`) |
| **Approval middleware** | Tiering, standing grants, sink checks, action binding | `authorize(tool, args, provenance) → allow/ask/block` |
| **Secrets** | Scoped credential access, never logged | `get(resource) → value` |
| **Capability substrate** | Author · verify · register · run authored capabilities | `author()`, `registry`, `runner` |
| **Resource registry** | Declarative external access (CLIs/APIs/devices) | `registry.toml` + provisioning |
| **Memory** | File-first facts + SQLite recall index | `write(entry)`, `recall(query)` |
| **Calendar** | The internal `Daedalus` calendar — file-first `.ics` + derived index | `CalendarStore`, the `calendar` primitive |
| **External integrations** | `gcal` (read-all / write-own); read-only mail is **deliberately last** (SPEC §10) | store API, `sync()` |
| **Setup wizard** | First-run provisioning, hardening, and the self-test | `scripts/setup.sh` ⟷ `daedalus selftest` |
| **Cabinet** | Seat identities, charters, namespaces, seat↔seat protocol | `Seat`, `Broker.send(msg)` |
| **Generative UI** | Compose UI spec, bind values locally, render, serve, tune | `render(context)`, control API |
| **Security spine** | Provenance labels, sink policy, quarantine | predicates used by middleware |

---

## 5. Repository & package layout

```
daedalus/                      # code repo
  pyproject.toml
  SPEC.md  ARCHITECTURE.md  AGENTS.md  CHANGELOG.md
  scripts/setup.sh           # first-run wizard (template.sh-based)
  src/daedalus/
    main.py                    # entrypoint: bootstrap + run daemon
    config.py                  # settings, tiers, paths
    loop/
      engine.py                # turn engine
      context.py               # context assembly
    model/
      client.py                # OpenAI-compatible base_url client
      providers.py             # NIM chain, z.ai, local runner
      ratelimit.py             # SQLite ledger
      routing.py               # heavy vs cheap (Bonsai)
    tools/
      base.py registry.py guard.py
      shell.py code.py files.py http.py browser.py search.py
      schedule.py process.py memory.py calendar.py discord.py ask.py secrets.py capability.py
    approvals/
      middleware.py grants.py risk.py classify.py
    capabilities/
      authoring.py manifest.py runner.py registry.py verify.py
    resources/
      registry.py provisioning.py
    memory/
      store.py index.py schema.py facets.py markdown.py namespaces.py
    calendar/
      store.py index.py event.py ical.py recur.py occurrences.py zones.py reminders.py
    scheduler/
      scheduler.py jobs.py wakes.py proactive.py catchup.py cron.py
    surface/
      discord/ bot.py approvals.py
      base.py cli.py
    cabinet/
      seat.py namespace.py protocol.py broker.py
    genui/
      spec.py components/ renderer.py theming.py dashboard.py server.py actions.py tuners.py publish.py
      tokens/approved.toml drafts/<id>.toml      # adopted values, pending tunes
    security/
      provenance.py sink.py quarantine.py env.py
    integrations/
      gcal.py mail.py calendar_sync.py
    storage/
      paths.py
    selftest.py                # end-to-end verification (setup + health)

$DAEDALUS_HOME/                # runtime state (default ~/.daedalus)
  config.toml  .env
  memory/       facets/*.md  index.sqlite
                namespaces/<seat>/facets/*.md             # one directory per other seat
  calendar/     events.ics  index.sqlite  sync_state.json
  capabilities/ <name>/{run.py, manifest.toml, README.md, state/, .venv/}
  resources/    registry.toml  creds/            # creds encrypted, scoped
  cabinet/      seats/*.toml  messages.db        # typed protocol log
  genui/        components/  tokens/  drafts/
  approvals/    grants.toml  log.db
  state/        wakes.db  ratelimit.db  proactive.json  reminders.json
  ui/                                        # generated dashboards (served)
  logs/                                      # journald mirrors
```

**Rule:** the repo holds code; `$DAEDALUS_HOME` holds state. Code is versioned; state is owned by the owner and editable by hand.

---

## 6. Runtime & process model

- **Daemon** — `daedalus.service` (systemd `--user`, `enable-linger`). Lightweight, always connected to Discord so DMs land; does **no idle reasoning on battery**.
- **Scheduler** — `daedalus-tick.timer` fires every N minutes → triggers `daedalus run`, whose in-process ticker pumps `dispatch_tick()` to drain due wakes/jobs. Cron-pumped, not a polling thread.
- **Capabilities** — spawned as **child processes**, each in its own uv venv, resource-capped, killed on timeout.
- **Power profiles** — AC: up to 3 workers, idle reasoning allowed. Battery: 1 worker, no idle reasoning.
- **Suspend** — sleep ⇒ no work; on resume ⇒ a single **wake digest** (missed wakes coalesced; no duplicate external side effects).
- **Lid closed ⇒ stays awake** (`logind`), so remote access and overnight work survive.
- **Logging** — structured logs to journald; per-capability audit log in `state/`.
- **Self-test** — `daedalus selftest` runs the end-to-end verification the setup wizard ends on (§7.7).

---

## 7. Core flows

### 7.1 Inbound turn

```
Discord DM ─▶ Surface.recv ─▶ Engine.run_turn(event)
  1. resolve surface + active seat identity → charter
  2. assemble context:
       charter + memory recall + capability inventory
       + resource inventory + thread history + tool schemas
  3. model.chat(...)                       [routed: heavy | cheap | local]
  4. while tool calls returned:
       approvals.authorize(tool, args, provenance) → allow ▸ run
                                                   → ask   ▸ present, park
                                                   → block ▸ refuse
       append results; model.chat(...)
  5. emit output ▸ Surface.send        (message | file | serve | push)
  6. write memory candidates; schedule follow-ups; close turn
```

**The active seat is resolved once, at step 1.** `pick(seats, seat)` where `seat` is the caller's (a wake names none). No name means the charter carrying `active = true` — the identity the owner chose, and choosing it is `daedalus activate`, a write to the charter files and nothing else. That single resolution sets the session, the charter in the prompt, and the memory namespace, so the three cannot disagree about who is answering.

### 7.2 Capability authoring (the tool-making flow)

```
request (free text)
  1. Engine detects a recurring/reusable need
  2. Model authors  run.py + manifest.toml + README.md  in genui-agnostic scratch
  3. Melinoë gate: run once in the capability's venv
       checks:  output shape · side effects · declared-scope compliance
  4. if the manifest needs egress ▸ owner approves (Discord button) → standing grant
       else ▸ registered directly
  5. registry.register()  ·  inventory refreshed
  6. if manifest.schedule set ▸ scheduler.jobs.add()
```

### 7.3 Scheduled capability run (no model in the loop)

```
daedalus-tick.timer ─▶ daedalus run (in-process ticker) ─▶ dispatch_tick ─▶ due job
  1. runner.spawn(capability)            → child process, own venv, caps
  2. run.py reads JSON context on stdin, writes JSON result on stdout
  3. runner records result + state/ checkpoint
  4. if result is notifiable: model composes the *message* (structure only)
  5. output ▸ Surface (message | push)
```

The artifact is deterministic; the model authored it once. Notification composition is the only optional model step.

### 7.3.1 The wake — the agent choosing to speak

```
cron wake job due ─▶ gate (deterministic) ─▶ briefing ─▶ model composes fresh ─▶ Surface
  1. proactive.may_speak(settings, now)
       quiet hours?      ▸ skip; record "stayed quiet — quiet hours (22:30–07:30)"
       cap spent today?  ▸ skip; record the reason
  2. runtime.wake() assembles a *briefing* from local signals
       the time · today's agenda · how often it has spoken · what it said last
  3. the model composes the message — or answers `[silent]`
  4. text? ─▸ Surface + proactive.note_spoken() ; empty ─▸ nothing sent, nothing spent
```

**The gate is in code; the substance is the model's.** Whether a wake may speak is settled *before* the model is reached — an agent that can argue its way past its own quiet hours does not have any. A capability *alert* never passes through here: that is not the agent choosing to speak, and it must fire when it fires.

### 7.3.2 A reminder — the owner's instruction, not the agent's choice

```
tick ─▶ calendar.due_reminders(now-15min, now) ─▶ unseen? ─▶ render ─▶ Surface ─▶ ledger
```

A third kind of outbound speech, alongside the capability alert and the wake: it
is a `VALARM` the owner set, so it **does not pass through the proactive gate** —
a 03:00 reminder the owner asked for fires at 03:00, where a *wake* would be
silenced. Its schedule comes from the calendar rather than a cron job: the tick
looks back over its own window for reminders that have come due, `state/reminders.json`
remembers what was delivered so a double tick cannot double-nudge, and a
`local_only` event's reminder still fires — it goes to the owner, over the
owner's surface, with no model in the loop.

### 7.3.3 The catch-up queue — what fell due while the loop was down

```
dispatch_tick(when)
  1. due jobs        cron.matches(when) — exactly this minute
  2. reminders       the last 15 minutes
  3. catch_up(when)  everything else that was missed, one at a time
       plan():  floor = last_run | created_at
                latest occurrence in (floor, when]  ▸ candidate
       too late?  ▸ record "missed 08:00 — not caught up: 6h20m late, past its 1h window"
                    last_run = now
       worth it?  ▸ stagger_s ▸ run it  ▸ record "caught up the missed 08:00 run"
```

A tick is only ever *now*, so a job whose moment passed while the daemon was down
was never asked about: not skipped, not recorded — from the scheduler's point of
view the minute simply never was. The queue answers for it, and *"only the needed
tasks"* is where the judgement lives:

| Rule | Why |
| --- | --- |
| Only the **latest** missed occurrence is a candidate; the rest are counted | 288 missed ticks of a five-minute job is one thing to say, and the newest one says it |
| A window decides whether even that one is worth it — `1h` for a capability, `12h` for a wake, `late_within` per job | a probe reports a *condition*: a disk check from six hours ago is a false report, not a late one. A wake is about a *day*, and it is still today at nine |
| The missed moment decides **what** runs; the present moment decides whether it may **speak** | a briefing caught up at 23:40 waits for the morning — quiet hours did not lapse because the daemon was down |
| A skip writes `last_run` exactly as a run does | the verdict is taken once, not re-litigated on every tick for the rest of the day |

It runs *after* the due pass and looks strictly before the present minute, so the
two can never claim the same minute — which is also why it can run on **every**
tick, and why a tick that merely **overran** recovers, not only one after a
restart. Staggering is the last word on the owner's ask: the runs were already
sequential, but `stagger_s` (2 seconds) keeps three notifications out of the same
second. A deep outage is one digest, not a queue of stale alerts.

### 7.4 Dashboard render, approve, tune

```
GET /dashboard ─▶ genui.render(context)
  1. gather context signals locally (time, calendar, activity, power, body…)
  2. model composes *structure* (components, layout, keys) — never values
  3. renderer binds local values + applies mood→token set
  4. emit self-contained HTML ▸ serve loopback ▸ tailscale serve
POST /tune  ─▶ tuners.set(pending)   (palette/density/motion/type + system knobs)
POST /approve ─▶ tokens.commit(pending)   ▸ owner-approved store
POST /action ─▶ approvals route (visual actions vs. Discord split)
```

**Status.** The calendar views are built: `/dashboard`, `/day/`, `/week/`,
`/month/` read the internal store through `genui/dashboard.py` and render the
owner's day. What is *not* yet built is the generative half — a model composing
structure from the full context signal set — and the three `POST` controls below,
which the routing table still answers as *"not built yet"*.

**The server.** `genui/server.py` binds the **loopback only** (`127.0.0.1` — not a
parameter, not a default) and serves the generated artifacts in
`$DAEDALUS_HOME/ui/`. `daedalus serve` starts it; publishing it to the tailnet is
a separate act (Tailscale Serve, **never Funnel**), because *open a port* and
*open a port to the internet* must never be the same keystroke.

Reads are open to whatever is already on the loopback — that is what loopback
means. **Writes are not:** every control `POST` carries a per-install token from
the owner-only secret store (`dashboard-token`), and a server whose token is unset
accepts **no** writes at all. *"It is only loopback"* is not authentication:
anything running as this user can reach the loopback, and a dashboard write is a
real action against the owner's calendar. The routing table (§9.5) is written
down, so a control that is designed but not yet built answers *"not built yet"*
rather than a 404.

**The calendar view.** `genui/dashboard.py` is the *producer*: it reads the
internal store and returns a spec, its local values, and the mood — and
`genui/server.py` serves it at `/dashboard`, `/day/`, `/week/`, `/month/` (each
optionally pinned to a date). The split matters because it is the same seam the
model crosses: the page's spec carries **keys** and the values are bound at render
time, so a generative dashboard can reuse the vocabulary without the owner's data
ever entering a prompt (§3.6). Two rules are worth stating because they look like
details and are not:

- **Occurrences, never definitions.** Each day is read with the store's own day
  addressing, so a repeating event appears on every day it falls and a moved
  instance appears where it was moved. A calendar built from definitions is
  correct on the day a rule began and wrong every day after.
- **`local_only` is shown here.** §6 withholds from *egress*. This page is the
  owner's own surface, on the owner's own loopback, with no model in the loop.

The page is written to `$DAEDALUS_HOME/ui/` on generation — today's becomes
`index.html` — so the artifact outlives the request that made it. The response
also carries the CSP **as a header**, not only as the `<meta>` the renderer
writes: a meta policy begins at the point it is parsed, which leaves the bytes
before it unprotected.

**The control strip.** A view served to a **verified session** — one bootstrap URL
(`/dashboard?t=<token>`) verifies the token, hands the browser an `HttpOnly`
cookie, and redirects, so no rendered document and no logged URL ever carries the
secret — is served with controls; every other view is read-only. The strip is
built by code in `dashboard.py`, deliberately **outside the spec**, so a page the
model authored carries no actions at all; and it is served under
`form-action 'self'` while a model-composed page keeps `form-action 'none'`.

**The controls.** `genui/actions.py` is the page's side of the approval split
(SPEC §3.6: *visual → dashboard, action → Discord*). A control is resolved to a
real tool call and put through the same `approvals.authorize` the loop's calls
get — the tool's own risk tier, scopes and assessment — so the page cannot go
around a decision the model would have to ask for. `allow` runs the tool on the
daemon's loop; `ask` schedules the Discord button and answers *"waiting"* at once
(an HTTP request must not hang open on a person); `blocked` is refused with the
policy's reason. The vocabulary is closed (`calendar.create|update|delete`), and
the controls run only where the daemon started the server — `daedalus serve` is a
read-only viewer and says so.

**The tuners.** `genui/tuners.py` is the theme's pending → approved path. A tune
writes `genui/drafts/<id>.toml` and moves nothing; `/draft/<id>` renders the real
dashboard with the pending set and the two choices under it; only `approve`
writes `genui/tokens/approved.toml`, which `resolve()` lays **over** whichever
mood is chosen. Every value is checked when tuned and again when adopted — the
approved file is the owner's, and a hand edit gets the same door. Unlike the
controls, this needs no surface, so both `serve` and the daemon attach it.

### 7.5 Seat-to-seat collaboration

Seats communicate over the **typed durable protocol** (§9.2) via the broker; messages persist in `cabinet/messages.db`. Example: `Icarus → Melinoë (verify) → Icarus (result) → Daedalus (summary)`. Deterministic code owns correlation, dedup, and delivery.

```
Icarus ──request/verify──▶ messages.db ──offer──▶ Melinoë
   ▲                    (correlation_id = request.id)        │
   └─────────────result   (causation_id = request.id)─────────┘
```

The queue is **at-least-once with dedup**, and the two halves are two different promises. *Delivery* is an offer, counted and reported: a seat handed the same message twice is told (`attempts=2`) that an earlier attempt did not resolve it, because the broker **cannot** know whether that attempt finished — asserting otherwise would be the exactly-once lie §9.2 rules out. *Dedup* is what the broker can see: a retry is the same message when it carries the same id, and the sender minted that id before its first attempt (`protocol.py`), so the second write stores nothing, leaves the row's place in the queue alone, and answers `duplicate=True`; the second *answer* to an already-resolved message reports `False` and never rewrites the first. Ordering is the row's own `seq`, so a retry cannot jump the queue; `thread(correlation_id)` replays an exchange in the order it happened, and a message that opened one carries its own id as the correlation — a correlation id nobody can query is a field, not a link.

### 7.6 Calendar sync

```
wake | cadence ─▶ calendar_sync
  1. pull:  gcal changes since sync_state.cursor  →  external calendars (gcal-authoritative)
             →  mirror into the internal store (read cache)
  2. push:  local changes on the `Daedalus` calendar (internal-authoritative)
             →  gcal events (create/update/delete, tombstones applied)
  3. write sync_state.cursor
```

Writes to gcal are **egress actions** behind a standing grant scoped to the `Daedalus` calendar. The dashboard reads and edits the **internal** store directly.

### 7.6.1 Import — the calendar arrives before the sync does

```
daedalus calendar import owner.ics [--replace] [--local-only]
  parse (one document, many VEVENTs) ─▶ stamp provenance ─▶ merge by (uid, recurrence_id)
     └─ VTIMEZONE blocks carried over once each; a VTODO is counted, not copied
```

The owner's real calendar exists *today*, in a file, and gcal is not wired — so
the one-way door is a command, not a sync. Identity is `(uid, recurrence_id)`,
the same pair the index keys on, so an override lands *with* its series and a
second run of the same file changes nothing. Foreign events keep the name of the
calendar they came from (`X-WR-CALNAME`) and are marked `authority="imported"`;
a file we wrote ourselves round-trips with its own provenance untouched.

It is deliberately **not** a tool. The `calendar` tool is scoped to `state`; an
import reads an arbitrary path, so the owner runs it and the agent never goes
looking for a file to swallow.

### 7.7 First-run setup

```
scripts/setup.sh  (bash wizard, template.sh-based)
  1–11  provision: preflight · layout · secrets · owner · models · runner
                  · discord · resources · trust · runtime · hardening
  12    verify:  daedalus selftest
                  → model · discord · memory · capability + Melinoë gate
                  · scheduler · dashboard · calendar (if configured)
  13    handoff
```

The wizard is the **owner-driven provisioning surface**: every secret enters through hidden prompts and lands in the encrypted store / `.env`; every resource is granted here, or later conversationally by re-running the same stages. It is idempotent and re-runnable (repair mode), and **stage 12 fails loudly** — success is never claimed with a red item.

---

## 8. Data model

### 8.1 Memory entry

```
id            ULID
namespace     the seat whose fact this is; `daedalus` is the root
facet         one of the modeled facets (§SPEC 5)
content       text
source_ref    pointer to origin (message id / file / url)
origin        provenance label: trusted_owner | local_derived | untrusted_source
confidence    0.0–1.0
valid_from    timestamp
valid_to      timestamp | null      # bitemporal
superseded_by id | null
created_at    timestamp
```

Store: `memory/facets/<facet>.md` (**source of truth**, Obsidian-compatible: YAML frontmatter + one list item per entry) + `memory/index.sqlite` (**derived** FTS5 cache, rebuildable from the notes; optional embeddings column). Validity is enforced on read — a superseded or expired entry is not recalled unless history is asked for.

**Namespaces are directories.** `memory/facets/` is the namespace `daedalus` — the default identity's, and where every fact written before seats had namespaces already lives. Another seat reads `memory/namespaces/<key>/facets/`. The directory *is* the namespace, so the filesystem lists them (`memory/namespaces.py`); a store handle is bound to one and every read narrows to it, while `rebuild_index` walks all of them at once. One `id` is unique across the tree, so a handle shares the root and the index with the others and only the boundary differs.

### 8.2 Capability manifest (`manifest.toml`)

```
name, version, description
entry        = "run.py"
io           = { input_schema, output_schema }
deps         = ["..."]              # materialised into the capability's venv
schedule     = "cron | null"
scopes       = { net = [...], fs = [...], secrets = [...] }   # the leash
caps         = { cpu, mem_mb, time_s }
trust_tier   = auto | ask | block
owner_approved_at = timestamp | null
```

### 8.3 Resource (`registry.toml`)

```
[[resource]]
name     = "gmail"
class    = "remote_api | local_cli | device"
access   = "oauth | token | local"
scopes   = [...]
cred_ref = "creds/gmail"           # encrypted at rest
added_at = timestamp
```

### 8.4 UI spec (declarative, model-authored)

```
{ "theme": "<token-set-id>",
  "layout": [ { "component": "stat-card", "key": "cp.rating", "span": 2 }, … ],
  "bindings": { "<key>": "<local-value-resolver-id>" } }   # keys only; values bound locally
```

### 8.5 Other stores

- `approvals/grants.toml` — standing grants (action-hash, scope, expiry).
- `approvals/log.db` — every authorize decision.
- `state/wakes.db` — `agent_wakes` + job state (idempotency keys).
- `state/proactive.json` — the day's proactive ledger: how often the agent has spoken, and what it last said. Hand-editable, so "why did you ping me at 23:00" has an answer that is not a guess.
- `state/reminders.json` — which calendar reminders have already been delivered, so a tick that runs twice in a minute does not nudge the owner twice about the same meeting. Pruned after a week.
- `state/ratelimit.db` — rolling-window provider accounting.
- `cabinet/messages.db` — typed seat messages.
- `calendar/` — `events.ics` (the `Daedalus` calendar, internal-authoritative) + `index.sqlite` + `sync_state.json` (gcal cursor/token); reads from other calendars are cached with their gcal-authoritative origin.

---

## 9. Interfaces & protocols

### 9.1 Tool protocol

```
Tool:
  name: str
  title: str                  # the human name, for the approval prompt
  schema: JSONSchema          # args
  risk: auto | ask | block    # the static tier; shell derives it from the command
  scopes: {net, fs, secrets}
  grantable: bool             # may an approval be remembered? (shell: no)
  title_for(args) -> str      # per-call headline; a multi-op tool names the op
  approval_labels(args) -> (affirm, deny)   # the buttons say the action
  assess(args) -> Assessment  # what this call does, and how bad it could get
  async call(args, ctx) -> Result
```

The model sees only `name + schema + description`. Risk, scopes, authorization,
and the danger assessment are **invisible to the model** and enforced by the
middleware.

### 9.2 Seat message protocol

```
{ id, correlation_id, causation_id, from, to, type, payload, created_at }
type ∈ request | ack | partial | result | verify | failure | cancel
```

**At-least-once with dedup** (never exactly-once). Deterministic broker owns ordering, retries, and delivery; seats own semantics. `from` and `to` are the *keys* (`melinoe`), never the display names — a message addressed to `Melinoë` is a letter to an empty room, so `protocol.py` refuses it and spells out the key. The Python attributes are `sender` / `recipient`: `from` is a keyword.

- **`id` is the sender's, minted before the first attempt.** A retry reuses it and is therefore recognised as the same message; an id minted inside the retry is a new message every time, and no dedup downstream can recover that.
- **Delivery is an offer, and it is counted.** A message is offered until it is *resolved*, so a seat can be handed it twice — and is told so, instead of being left to guess whether the earlier attempt finished.
- **Resolution happens once.** The second `resolve` of the same id is the duplicate of the first and never rewrites it. `reopen` puts a *wrong* answer back on the queue and moves the count, because a rejected answer was still an attempt.
- **`correlation_id` names the exchange, `causation_id` one hop back.** A reply inherits the correlation and points its causation at the message it answers, so a thread is one query and "why did you say this" is a hop.

### 9.3 Capability run protocol

Process boundary, JSON in / JSON out:

```
stdin  : { args, context, state }
stdout : { result, state_next, notifications[] }
exit   : 0 ok | non-zero ⇒ failure recorded, monitored
```

Isolation is by process + venv + resource caps; the manifest declares the only scopes available.

### 9.4 Model client

```
chat(messages, tools, stream=True) -> Stream[Delta]
route:  heavy (NIM stepfun-ai/step-5-preview → z-ai/glm-5.3-flash
              → Z.ai glm-4.7-flash → glm-4.5-flash, 60s stall ceiling each)
      | cheap (Bonsai 8B, local)
      | forbidden-for-local-only (refuse remote when provenance = local_only)
```

### 9.5 Dashboard control API (loopback only, tailnet-authenticated)

```
GET  /dashboard          → HTML (generated); `?t=<token>` verifies once and
                           hands the browser its session cookie
GET  /day|/week|/month/<date>  → that view, generated on request
GET  /draft/<id>         → live preview of a pending theme/component
POST /tune               → set pending token/knob values
POST /approve            → commit pending → owner-approved store
POST /action             → one control (calendar.create|update|delete) through
                           the approval split; started with the daemon only
```

---

## 10. Security architecture

**Provenance flow.** Every value carries a label. Trusted work is `trusted_owner` or `local_derived`; anything fetched is `untrusted_source`.

```
untrusted_source ─▶ quarantine.read ─▶ local_derived (may inform a draft)
                                        ╳  may NOT authorize recipient/URL/command/scope
```

**Sink policy.** The egress sink checks the provenance of the *authorizing* value, not just the payload: untrusted text can shape content but can never set a recipient, URL, command, or scope. This is the same rule the Generative UI uses (*structure remote, values local*).

**Approval split.**

- **Action** approvals (egress, purchases, irreversible ops) → **Discord buttons**.
- **Visual** approvals (themes, components, dashboards) → **on the artifact** in the dashboard.
- Approvals bound to **action hash + scope + expiry + idempotency**.

**Danger classifier** (`approvals/classify.py`). Which tier a call falls in is
decided **from the call itself**, deterministically — never by the model, and
never by a model round-trip, because an approval prompt must not be something the
agent can stall, reword, or talk its way past. Every tool declares, for a given
call, what the call does and how bad its worst plausible outcome is:

```
ok            reads state; nothing changes                  ▸ auto
caution       changes something, reversibly                 ▸ ask
destructive   destroys data or history; not recoverable     ▸ ask, loudly
catastrophic  can take the machine with it, or reaches a    ▸ refused outright
              path the owner put out of bounds                 (never offered)
```

The tier drives the risk the middleware sees: `ok → auto`, `caution |
destructive → ask`, `catastrophic → block`. Two consequences are load-bearing:

- **A prompt has to carry information.** If every command asks, then asking stops
  meaning anything. Read-only commands run; what remains asking is exactly what
  can change something.
- **A refusal is the owner's business.** A blocked call is reported to the owner,
  because silently stopping means the agent tried something it was not allowed to
  do and nobody ever knew.

**Consent is narrow.** An approval is remembered as a standing grant only when it
was **not** destructive *and* the tool's scope actually describes what was
approved. `shell` is never grantable — no scope describes "whatever command I run
next", so its approval covers one command. A multi-operation tool scopes by
operation (`capability:author` ≠ `capability:remove`). The rule: *consent for
something irreversible is not consent for it later, forever.*

**Environment scrubbing** (`security/env.py`). Commands the agent runs inherit a
scrubbed environment: the install's own `.env` keys and anything whose name looks
like a credential are removed. A command's output flows back into the conversation
and on to a model provider, so the ambient environment is a place a value could
leave from — *structure travels, values stay local* applies to the process
environment too, not only to request bodies.

**Restraint rules** (`SPEC.md` §6, `AGENTS.md`): reach narrow; personal data out of bounds; bytes stay home; structure travels, values stay local.

**Secrets.** Stored encrypted under `resources/creds/`, accessed only through the `secrets` tool, scoped per resource, never logged, never sent to a remote model, never on Discord.

---

## 11. Capability substrate details

- **Registry** — the set of authored capabilities, listed to the model at startup (name · description · io · schedule). File-first; hand-editable.
- **Runner** — spawns `run.py` in the capability's uv venv with declared scopes and caps. **The caps are enforced, not recorded:** `time_s` is a wall-clock timeout; `cpu` is CPU affinity over `floor(cpu)` whole cores (a cap is never rounded *up*); `mem_mb` is enforced on **resident** memory by sampling `/proc/<pid>/status` while the artifact runs, because address space is not memory — a tight `RLIMIT_AS` kills a healthy CPython for reserving what it never touches (SPEC §13). A generous `RLIMIT_AS` remains only as a backstop for one allocation large enough to land between samples. The child is its own session leader, so a timeout or a breach takes everything it spawned. Owns the kill switch and the audit record, which carries the peak MB.
- **Authoring** — the model writes the artifact; the *engine* owns registration and gating. No hand-coded feature may be required to satisfy a request.
- **Lifecycle** — `born → verified → scheduled → monitored → re-authored | killed`. Daedalus audits capabilities like projects.
- **Growth** — proposals only; authoring requires an explicit owner yes. Never autonomous.

**The invariant:** any request resolves to a new **capability** (logic), a new **resource** (access), or — rarely — a new **primitive**. Never a hand-coded feature.

---

## 12. Generative UI subsystem

- **Spec (model-authored)** — components + layout + keys. No markup, no values.
- **Component registry** — bounded and **extensible**; a component is itself an authored capability, verified before use.
- **Theming engine** — `mood + context → validated token set` (palette/type/density/motion). Owner-approved; overridable.
- **Renderer** — spec + locally-bound values + token set → **self-contained HTML/CSS** (sanitized). The model never emits markup.
- **Server** — loopback HTTP, published via **Tailscale Serve** (no Funnel); output also pushable via Taildrop.
- **Control surface** — tuners for both presentation and system knobs; `approve` commits to the owner-approved store.
- **Calendar view** — a full day / week / month calendar over the **internal** store, used the way gcal is used (create / edit / delete), with edits routed through the approval split.

---

## 13. Failure modes & recovery

| Failure | Handling |
| --- | --- |
| Provider outage / rate limit | fall through NIM chain → z.ai → **Bonsai 8B local**; ledger enforces the rolling window |
| Capability breaks (site change) | monitor detects bad output → mark stale → model **re-authors** → re-verify |
| Laptop slept through a wake | wakes coalesce into a single **resume digest**; idempotency keys prevent duplicate side effects |
| Egress denied | action parked as a proposal; surfaced as a button; never silently dropped |
| Untrusted content tries to act | sink policy blocks authorization; content quarantined to `local_derived` |
| Capability runaway | resource caps + timeout + kill switch; audit record written |
| Secrets leak attempt | `secrets` tool refuses outbound embedding; bytes-stay-home predicate halts the action |

---

## 14. Extension points

Every extension lands at a single seam, in this order of preference:

1. **New capability** — author a script (most requests).
2. **New resource** — provision access (one-time, owner-driven).
3. **New component / token** — extend the UI vocabulary (owner-approved).
4. **New primitive** — only if irreducible; a new `Tool` implementation + schema.
5. **New seat** — a charter + memory namespace over the existing loop.

Adding any of 1–5 must **not** require editing the loop.

---

## 15. Phasing

| Phase | Delivers | Components touched |
| --- | --- | --- |
| **P0** | the loop + substrate + proof-of-life | engine, model client, primitives, approvals, Discord surface, capability substrate, memory v0, scheduler v0, Melinoë gate |
| **P1** | memory depth — file-first source of truth, supersession + bitemporal, `local_only` egress gate, Obsidian-compatible notes, rebuildable index | `memory/*` |
| **The internal calendar** | file-first `.ics` + derived index + the `calendar` primitive; `gcal` still unbuilt | `calendar/*`, `tools/calendar.py` |
| **P2** | proactive scheduler — quiet hours and a daily cap in code, a wake briefing from local signals, the choice to stay silent | `scheduler/*`, `config.py` |
| P3 | seats, one at a time | `cabinet/*` |
| P4 | remote / Tailscale Serve | `genui/server.py`, `surface/*` |
| P5 | OpenCode delegation | `resources` (local CLI), `capabilities` |
| P6 | group chat | `cabinet/broker.py` |
| **Mail (read-only)** | deliberately **last**, after P6 (SPEC §10) | `integrations/mail.py` |

---

## 16. Open questions

Tracked in `SPEC.md` §13 (runner for Bonsai 1-bit, embedding choice, manifest schema, resource provisioning flow, browser choice, GenUI spec schema, mood→token mapping, regeneration trigger, tuner set, `process` supervision). No architectural decisions are left open — they are implementation details behind the interfaces above.
