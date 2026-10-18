# Daedalus — Spec

**Scope:** single-user, local-first (only the owner can talk to it)
**Status:** P0, P1, P2 and the internal calendar implemented as code (loop + primitives + capability substrate + memory depth + calendar + scheduler + proactive gate + Melinoë gate + Discord surface + setup wizard), with the P3 substrate under it (charters as files, a memory namespace per seat, one active identity, and the durable seat↔seat queue); 845 tests green, `daedalus selftest` 15/15 offline. The **proof-of-life (§11) holds** — observed from Discord on 17 Oct, a free-text request authored and scheduled a capability with no code touched.
**Date:** 2026-10-16
**Supersedes:** the Hermes-based `~/down/daedalus.md` plan (architecture reference retained; runtime decision reversed)

---

## 0. Purpose

A single-user, local-first **chief-of-staff agent** that runs on the owner's laptop and is reached over Discord. It coordinates priorities, memory, scheduled work, and a cabinet of specialist identities. It is hyperpersonalized to one person: behaviour is composed at runtime, never shipped as fixed flows.

---

## 1. Design Principles

0. **Free text in. Capability, not flow. No hardcoded behaviors.**
   The owner says what they want in plain language; the model composes the *behavior* at runtime out of capabilities (memory, tools, scheduling, research, code execution). The system is extended by **talking to it** or handing it a new tool — never by editing a script. Deterministic code owns **only hard boundaries** (auth, risk, budgets, secrets) — never *what* it decides to do.

1. **The model proposes, a deterministic engine disposes.**
   LLMs never own the hard boundaries.

2. **Local-first.** Secrets never leave the machine and never touch Discord. Discord is never the transport for private data.

3. **Depth for one person.** Hyperpersonalized to a single owner — not a generic assistant.

4. **Bias to action.** Convert plans into the next concrete shippable action; surface and kill stale work loudly.

---

## 2. Non-goals for P0

The cabinet, theming, remote/Tailscale access, OpenCode delegation, and the multi-agent group chat are **out of scope for P0** — P0's gate on them ("until the loop does a real thing that was not hardcoded") was met on 17 Oct (§11), so they are now a matter of phase order (P3 → P4 → P5 → P6), not of permission.

---

## 3. Harness & Stack

- **Custom, standalone harness.** Hermes is **out** — it is divergent from what is wanted and does not fit.
- **Python 3.14 + uv.**
- **One loop:** Discord in → model → tools → output, plus memory, scheduler, and approval middleware.
- The **cabinet seats are durable identities with charters and separate memory namespaces over the same loop** — *not* eight running processes. Lazy, budgeted, suspend-aware.

**Retained from `daedalus.md` (deep architecture reference):**
- Coordination adapter + **typed durable message protocol** (`request / ack / partial / result / verify / failure / cancel`, with correlation/causation IDs) — how seats (e.g. Athena ↔ Odysseus) collaborate.
- Provenance labels (`trusted_owner | local_derived | untrusted_source`) and data classes (`public | private | local_only`); **sink policy** (source text may inform a draft but can never authorize a recipient/URL/command/scope); quarantined reader for untrusted content.
- Risk tiers with allow/ask/block; approvals bound to action hash / scope / expiry / idempotency.
- AC-vs-battery power profiles; sleep → no work; wake digest on resume; no duplicate external side effects.

---

## 3.5. Capability Substrate — the tool-making machine

The system is **not** a fixed toolbox. It **authors its own tools** on demand, keeps them, and runs them on a schedule: a capability is built on request, then owned and reused.

**The invariant.** Anything asked resolves to **(1) a new capability** (logic), **(2) a new resource** (access), or — rarely — **(3) a new primitive** (only if truly irreducible). Never a hand-coded feature. Generalization has two axes — *synthesis* over logic (unbounded, because `code`) and *provisioning* over access (additive, one-time) — and one boundary: **physical reach and access that cannot be provisioned**.

### Layer 1 — Primitives (always present)

| Tool | Does |
| --- | --- |
| `shell` | run commands (scoped, audited) |
| `code` | author & execute code (Python / Node / Bash) in a working dir; install deps |
| `files` | read / write / glob / grep |
| `http` | fetch, download, POST |
| `browser` | headless Chromium — JS-heavy sites, DOM extraction, screenshots |
| `search` | web search |
| `schedule` | cron-like: run *anything* on a period |
| `remind` | a one-shot wake: a reminder for one named moment, kept once (T42) |
| `process` | start / stop / monitor long-running processes (servers, demos) |
| `memory` | read / write facts |
| `calendar` | the internal Daedalus calendar: agenda, add / change / delete (§5.5) |
| `discord` | send messages, incl. proactive |
| `ask` | approval requests as buttons |
| `secrets` | scoped credential access |

### Layer 2 — Authored capabilities

When a request is recurring or reusable, the model **writes a capability** instead of reaching for a pre-built tool:

```
capabilities/<name>/
  run.py          # the deterministic artifact the model authored
  manifest.toml   # name, deps, io schema, schedule, scopes (net/fs/secrets), trust tier
  README.md       # why it exists
  state/          # outputs + checkpoints (e.g. "last-seen item")
```

- File-first, inspectable, versioned, hand-editable.
- **Own uv venv per capability** — dependencies never collide.
- Registered in the file-first store; **listed to the model at startup** so it knows what it has.
- **Scheduled runs execute the artifact directly — no model.** The model authored the deterministic code; the runtime disposes. Cheap, reproducible, auditable.
- The model **re-authors** the capability when the world changes (e.g. a site redesign breaks a scraper).

### Output channels

How the agent hands artefacts **to the owner** — delivery, not action:

- **`message`** — Discord, incl. proactive.
- **`file`** — write / send.
- **`serve`** — spin up a loopback HTTP server, published privately via **Tailscale Serve** (loopback-bound, **no public Funnel**). This is how a served project, a dashboard, or the **calendar view** reaches the phone. The daemon's own dashboard binds a **fixed port** (`[ui] port`, default `8787`) and is published at start and taken down at stop, so the route always points at a live port; `[ui] publish = false` keeps it loopback-only.
- **`push`** — send to an owner device via **Taildrop** (e.g. the phone).

Forward (not built yet): **voice / audio**, **live / streaming views**, **non-Discord notification transports**.

### External actions

How the agent acts **on other systems** — always through a resource (Layer 3), always trust-gated, never an "output":

- **mail** — read + summarize only (§5.5).
- **calendar** — read all, write the `Daedalus` calendar (§5.5).
- **git** — push, open a PR / issue.
- **social** — posts *(forward)*.
- **payments** — *(forward, approval-heavy)*.
- **data** — DB / spreadsheet writes *(forward)*.

### MCP path

Authored capabilities are local scripts first. The design leaves a clean seam to MCP:
- **Consume:** register external MCP servers as additional tools alongside the primitives.
- **Expose:** publish the capability registry over MCP so other agents (e.g. the OpenCode delegate) can call the owner's authored capabilities.
Nothing in P0 depends on this — it is a seam, not a build.

### Execution posture — host-native, kept light

- Authored code runs **directly on the laptop** — single-user, local-first.
- The **manifest is the leash**: it declares the capability's network / filesystem / secret scopes and resource caps (CPU / memory / time).
- **Audit log + kill switch** on every capability.
- **Egress is gated**: a capability's first network action needs the owner's one-time approval, then becomes a standing grant.
- Containers are reserved for genuinely *third-party* code — never for the owner's own agent scripts.

### Verification gate (Melinoë)

Every **newly authored** capability is **run once and checked by Melinoë before it is scheduled** — output shape, side effects, scope compliance. No unverified automation runs unattended. Re-authoring re-triggers the gate.

### Layer 3 — Resource registry

External access is a **declarative registry** (parallel to the capability registry), provisioned *per tier* and **added by talking to the agent**, not by coding. A resource = access + credentials, provisioned once, listed to the model at startup. Three classes:

- **Local CLIs** — `opencode`, `tailscale`, `docker`, `git`, local runners.
- **Remote APIs** — mail (`readonly`), calendar (**read-all / write-own**, §5.5), competitive-programming APIs, hosted search.
- **Devices** — phone (Taildrop / Tailscale), webcam.

P0 wires only the scraper substrate: `search`, `browser`, `http`, `uv`/`npm`. Everything else is added later, each gated by trust.

**Provisioning posture: owner-driven.** The agent detects the need and prepares the OAuth URL / exact install line; the owner completes it locally in one step. The hand that grants access is the owner's.

### Capability lifecycle

Unbounded authoring without a lifecycle is sprawl. Every capability moves through a defined lifecycle:

**born → Melinoë-verified → scheduled → monitored (still working?) → re-authored or killed.**

Daedalus audits capabilities the same way it audits projects: an authored capability that no longer earns its place is killed loudly.

**Growth posture.** It notices a capability the owner would want and proposes it, authoring only on an explicit yes. Never autonomous authoring.

---

## 3.6. Generative UI — dashboards

The dashboard is **not a template**. It is **generated per render** from context — time, calendar, activity, task, power state, body, weather — so what opens is assembled for *this* moment, in the owner's theme, for the owner's mood.

Reconciling "truly generative" with the rule *"never arbitrary model-generated CSS/JS"*:

> **The model proposes a UI spec; a deterministic renderer disposes.** The model composes from a bounded, **extensible** vocabulary of components + validated theme tokens and emits a **declarative spec**. A renderer turns that into sanitized, self-contained HTML/CSS. The model never emits raw markup. It is generative in **composition and styling choices**, bounded in **rendering**.

- **Owner-approved.** The component library and token set are the owner's. New components/tokens are **proposed** and **approved** before use — the vocabulary grows deliberately, not by drift.
- **Component vocabulary** — `stat-card`, `chart`, `list`, `timeline`, `calendar`, `streak`, `progress`, `table`, `prose`, … each with a schema. **Components are themselves authored capabilities** — the visual vocabulary grows the same way scrapers do (§3.5), and is Melinoë-verified.
- **Custom theming engine** — a declarative engine maps **mood + context → a validated token set** (palette / type / density / motion), carrying the owner's aesthetic. Starting moods: `calm`, `midnight-focus`, `dense-ops`, `low-energy`, `celebration`. **Mood is a first-class context signal** and may also shape *how Daedalus and Calliope speak*. The owner can override at any time.
- **Design system: shadcn/ui's** (decided 17 Oct) — a **fixed set of primitives** every page composes from, with a real spacing/radius/shadow/type scale, so consistency comes from the vocabulary rather than from discipline. The design language lands first, inside the no-build, no-script property above; the React/Tailwind toolchain is sanctioned for the day the owner wants complex layouts, recorded then as a decision that supersedes that property rather than eroding it.
- **Context signals — all in-bounds:** time / day / season, calendar, active task / thread, recent agent activity, battery / AC, focus / quiet hours, weather, body data, financial / application pipeline.
- **Structure remote, values local.** The model composes **structure** — which components, which layout, which keys — never the sensitive **values**. Data binding happens **locally** in the renderer, so `local_only` values never enter a remote prompt. A dashboard that inherently needs local-only values *in the prompt* is composed by a **local model**. This is the §6 sink policy applied to the UI.
- **Output** — self-contained HTML; served loopback + **Tailscale Serve** (`serve`), pushable via **Taildrop** (`push`) — §3.5 output channels.

### Approval & tuning surface

- **Approval-by-rendering.** Visual artifacts — themes, components, dashboards, and the dashboard itself — are approved **on the served artifact**, not in chat: the proposal renders **live**, the owner tunes it in place, and approval is a control on that page. *Seeing is the most accurate way to say yes.* Action approvals (egress, purchases, irreversible ops) stay as **Discord buttons** (§6). The split is: **visual → dashboard, action → Discord.**
- **Control surface / tuners.** The dashboard is interactive; controls call back into the agent.
  A control is a **proposal, never an act**: every control POSTs to `/action`, which resolves it to
  a real tool call and puts it through the **same approval middleware the loop's tool calls get** —
  the tool's own risk tier, scopes, and assessment — so the page cannot go around a decision the
  model's calls would have to ask for. The control vocabulary is closed (create / update / delete on
  the calendar) and is built by code, not by the model, so a page a model authored carries no
  actions at all. A control that needs the owner answers *"waiting"* immediately and surfaces its
  **Discord buttons**; the request never hangs open on a person. **Tuners** adjust the live theme
  (palette / density / motion / type) and, by extension, **system knobs** (schedules, thresholds,
  quiet hours, seat behaviour). Tuned values form a **pending** set; **approve** adopts them into
  the owner-approved store. Drafts, approvals, and the token store persist locally.
- **Security.** Served over the tailnet, single-user, tailnet-authenticated. Write actions
  (tune / approve / a dashboard control) are gated like any other action — **no anonymous
  controls**: the per-install token is required, the browser gets it once through a verified URL
  and holds it as an `HttpOnly` cookie, so no rendered document and no logged URL ever carries it.
  A model-composed page keeps a `form-action 'none'` policy; only the code-built control strip is
  served under `form-action 'self'`.

---

## 4. Models

- **Primary:** NVIDIA NIM free tier, model-agnostic behind one OpenAI-compatible `base_url`.
  Fallback chain, **as verified against the live catalogue during P0**:
  **`stepfun-ai/step-5-preview` → `z-ai/glm-5.3-flash` → Z.ai `glm-4.7-flash` → Z.ai `glm-4.5-flash`**.
  Both NIM entries were confirmed to return text *and* well-formed tool calls; the **fastest entry is
  first**, because a cold primary that stalls is worse than a warm fallback, and each entry carries a
  **60s stall ceiling** (`timeout_s`) — the client streams, so this bounds silence, not answer length.
  *Superseded:* the earlier shorthand `glm-5-3 → glm-4.7 → minimax → kimi` named models that do not
  exist — `glm-4.7` and `minimax` are absent from the catalogue, `moonshotai/kimi-k2.6` is listed but
  answers 404, and `z-ai/glm-5.3` (non-flash) does not answer within a useful timeout.
- **Second free stack:** Z.ai direct (`glm-4.7-flash`, free; `glm-4.5-flash` as its fallback).
- **Local:** **Bonsai 8B (1-bit)** for offline fallback *and* cheap/dumb tasks. Run via a local runner (Ollama or llama.cpp) — **exact runtime support for the 1-bit weights to be verified at P0.**
- Heavy local models are **dropped** — they do not fit the available RAM, and the latency is unacceptable.
- **No Nemotron.**

---

## 5. Memory

- **File-first, and the file wins.** The markdown under
  `$DAEDALUS_HOME/memory/facets/<facet>.md` is the **source of truth**; the SQLite
  FTS5 index is a **derived cache** that can be rebuilt from the notes
  (`MemoryStore.rebuild_index`). An owner editing a facet by hand is not fighting a
  hidden second copy of the truth.
- **Memory is namespaced by seat.** Reads and writes are scoped to the seat the turn
  runs as, so what one identity knows is not what another knows. The namespace is a
  *directory*: `memory/facets/` is the default namespace (`daedalus`) — where every
  fact recorded before seats had namespaces already lives — and any other seat reads
  `memory/namespaces/<key>/facets/`. A charter may name its namespace, but `daedalus`
  cannot be pointed elsewhere, because moving it would orphan every fact already
  written. The index carries the namespace and `rebuild_index` walks every namespace,
  so an index written before the column existed is **rebuilt rather than guessed at**,
  and re-reading the notes leaves every seat's memory where it was.
- Every entry carries **provenance, confidence, and supersession**, and is
  **bitemporal** — `valid_from` / `valid_to` plus `superseded_by`. Superseding a fact
  retires it: recall returns what is true *now*, history requires
  `include_invalid=True`, and the retirement survives an index rebuild.
- **`local_only` never reaches a remote model.** A fact marked `local_only` may
  enter the prompt or a recall result only when the **entire resolved model chain is
  local**; a single remote fallback is enough to withhold it. The decision is made
  from the chain, never by the model.
- Untrusted text can never launder instructions into trusted memory (§6 sink policy).
- **Obsidian bridge.** Each facet is a valid vault note: YAML frontmatter
  (`facet:` and `tags: daedalus/memory/<facet>`) and one list item per fact, metadata
  in a hidden HTML comment. Point a vault at `memory/facets/` and the notes are
  readable and hand-editable; a bullet written by hand is adopted into the index
  with its own stable id.
- **Recall is FTS5.** The embedding choice is recorded as resolved in §13:
  embeddings remain a seam behind `MemoryStore.recall()`, not a build.
- **Facets modeled for the owner:**
  - competitive-programming **rating + contest calendar**
  - **aptitude / DSA** practice reps
  - **body**: weight, workouts, sleep
  - **income target + application pipeline**
  - **people + contacts**
  - **projects + status**
  - **research thread**
  - **published posts / brand**
- **Capture is prompted, not hooked.** No extraction pipeline watches the
  conversation; what makes a durable fact land is the system prompt naming the
  habit (T43): people, contacts, dates, commitments and stated preferences are
  written in the turn they are said, superseded rather than contradicted, and
  banter is not memory. The store's own guarantees (§5 above) are what keep a
  prompted capture honest.

---

## 5.5. Calendar, mail & external integrations

**Internal calendar — a first-class store.** `$DAEDALUS_HOME/calendar/` — file-first `.ics` + a SQLite index. This is the agent's own calendar and the owner's primary interface to it. It is also a **context signal** (§3.6) and a **dashboard view** (§3.6).

**Built.** `calendar/events.ics` is written by `daedalus.calendar.CalendarStore` and reached
through the `calendar` primitive (Layer 1). It is genuinely file-first: every mutation
re-reads the file before changing it, so an event the owner added by hand — and any property
line the module does not model — survives a tool write, and `index.sqlite` is a derived cache
that rebuilds itself whenever `events.ics` changes underneath it. Times are **floating
local** unless an event names a `TZID`, in which case the zone travels with the time; an
event's shape decides whether it is all-day (`2026-10-16`) or a moment
(`2026-10-16T09:10`). Events carry a data class, so a `local_only` event is withheld from any
prompt that could reach a remote model, exactly as memory is. `gcal` is **not** built: it
remains the read-all / write-own resource below, and `sync_state.json` is its empty cursor slot.

**A repeating event is a definition; a read answers with occurrences.** One `VEVENT` may carry
an `RRULE` (`DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`, with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`
including ordinals such as `-1FR`, `BYMONTHDAY`, `BYMONTH`, `BYSETPOS`, `WKST`); `EXDATE` leaves
instances out; a `RECURRENCE-ID` override — a second `VEVENT` sharing the series' `UID` —
replaces exactly one instance. The recurrence set is the RFC's: **`RRULE ∪ RDATE − EXDATE`** —
an `RDATE` adds a date the rule does not produce, with or without a rule, and an `EXDATE`
removes one however it got there. `agenda` and every other read expand the rule and answer in
dated occurrences, so "every weekday at 9:10" answers for the day being asked about rather than
existing as a rule nobody can read. Writes say what they mean: `scope=all` edits the series,
`scope=this` the one instance, `scope=future` splits the series at that point. A `VALARM` is
kept as an offset from the start, and `TRANSP` records whether an event makes the owner busy.
Driving gcal itself is deferred: the owner's calendar is the one on this laptop.

**The calendar answers questions, not just lists.** `busy` returns the merged stretches the
owner is not free in (anything `TRANSP:TRANSPARENT` is on the calendar without blocking it),
`free` returns the gaps of a given length inside each day's waking hours, an overlap between
two events is reported as the double-booking it is, and `search` finds events by their text
over an FTS5 index. All of it works on **occurrences**, so a weekday standup blocks that slot
every weekday.

**Reminders fire, and fire once.** A `VALARM` is an offset from the start, and the scheduler
tick looks back over its own window for reminders that have come due, so one the owner set
arrives on time and is not repeated (`state/reminders.json`). A reminder is the owner's
instruction rather than the agent's choice to speak, so it does **not** pass through the
proactive gate (§9): a 03:00 reminder they asked for fires at 03:00, where a *wake* would be
silenced. The `remind` verb (T42) extends the same principle to the agent's own wake path: a
reminder for **one named moment** — `within` ('2h') or `at` (a timestamp) plus a note —
composes fresh as a wake, does not pass through the proactive gate (the owner named the
time), fires once, and then retires out of the job store, so no absolute-date cron can turn
"send your writing tomorrow" into a yearly event. A delivery far past its moment **announces
the lateness** rather than pretending it was on time, and a compose that fails keeps the
reminder and retries rather than retiring an owner's alarm into silence.

**A change can reach a whole set.** `update` and `delete` take one event by `uid`, or every
event whose **name** matches `match` — "move all my standups to 10" is one call, not a loop
the model has to build out of ids it guessed at. The time is given as `at` (`"10:00"`), which
applies to each event's **own** date: a shared date would pile a week of standups onto one
morning, which is the failure mode that makes bulk editing worse than doing nothing. Matching
is on the summary alone, so an event that merely mentions the word in its notes is not swept
along, and the approval prompt names the set it is about to move.

**A set can be addressed by the day it falls on, and moved by a date delta.** `match` names a
set by *what it is called*; `on` (`"2026-10-25"`) names one by **when it falls** — the events
that produce an occurrence that local day, with or without a name to narrow them. `on` on its
own is the whole address, which is what "move the two things on Sunday" needs when the things
are called different names. A date move is `shift`, a signed day or week offset (`"-7d"`,
`"2w"`): each matched event keeps its own time of day and length and moves by the delta, where
`at` sets a clock and `start` names one shared date. This is the tool's answer to "nah, wrong
Sunday — I meant the coming one", where the owner's two Sunday events share no word to match
on. `delete` takes the same `on` addressing. Both are bulk writes, so both still pass the
`CAUTION` approval that names the set, and the day-addressed set is filtered through the same
`local_only` gate as every other read: on a remote-bound turn the model cannot address an event
it was not allowed to see.

**A series is several entries, and a change moves all of them.** One repeating event is a rule, an
`EXDATE` per skipped day, an `RDATE` per added day, and a `VEVENT` of its own per instance the owner
edited — each with its own `DTSTART`, joined by the series' `UID` and pinned by `RECURRENCE-ID`. So a
change that moves the rule by a delta moves those with it, by the same delta: an instance moved to the
13th is *the 12th's* instance, and shifting the series a week back puts it on the 6th. Moving the rule
alone is wrong twice at once, in opposite directions — a skipped day comes back (its `EXDATE` now names
a date the rule no longer produces, and it looks like an ordinary event) and an edited instance is
stranded where it was (which reads as one more thing on). `UNTIL` is a date *inside* the rule and moves
with it; `COUNT` counts from a `DTSTART` that is moving too, so it needs nothing. An explicit new
`start` is a translation as well, and carries the same exceptions. A `scope="future"` split gives the
`VEVENT`s at or after the cut to the new series, because an override belongs to the rule it excepts.

**Clocks other than this one.** An event may name a `TZID`; the zone travels with the time, and
`recur.py` stays pure wall-clock arithmetic, so "9:00 every weekday" stays 9:00 in the zone it
was written in across a daylight-saving change. Real instants — not wall-time strings — are what
free/busy and overlap compare, so an event on a foreign clock lands on the right day here; a zone
name shown on a line is shown only when it is not this machine's. An unreadable zone falls back
to floating rather than guessing an offset.

**Import — the calendar arrives before the sync does.** `daedalus calendar import <file.ics>` reads a
whole calendar document (a Google Takeout export is the case it was written for: many `VEVENT`s,
one `VTIMEZONE` per zone, `TZID`s, rules, alarms, overrides) and merges it into the internal store.
Identity is `(uid, recurrence_id)` — the same pair the index keys on — so an override lands with the
series it belongs to, and **re-importing the same file changes nothing** unless `--replace` says to
overwrite. `VTIMEZONE` blocks travel with their events, once each; a component the store does not
model (`VTODO`, `VJOURNAL`) is counted and left alone rather than silently copied. Imported events
keep the name of the calendar they came from and are marked `authority="imported"`, so nothing here
claims the agent wrote them; a file *this* store wrote round-trips with its own provenance intact.
`--local-only` marks the whole import withheld from any remote-bound prompt (§6), for a history the
owner wants searchable but not quotable. It is a **command, not a tool**: the `calendar` primitive
is scoped to `state`, and an import reads a path of the owner's choosing.

**`gcal` — a resource, read all / write one.**

- **Read** every calendar the owner has — context for the day.
- **Write** only a dedicated **`Daedalus`** calendar it owns — never a personal calendar.

**Authority split — no heuristics:**

- `Daedalus` calendar → **internal-authoritative**; gcal mirrors it.
- Every other calendar → **gcal-authoritative**; the internal copy is a read cache.

**Sync.** Cursor/token-based: pull changes since the last sync, push local changes; **tombstones** for deletes so a sync cannot resurrect them. Triggered on wake and on a cadence. Writing is an **egress action → standing grant**, scoped to the `Daedalus` calendar.

**Dashboard.** The internal calendar has a **full dashboard view** (`serve`) — day / week / month, create / edit / delete — used the way gcal is used. Controls route through the approval split (§3.6).

The three views are read paths on the loopback server: `/dashboard` is today, and `/day/`, `/week/`, `/month/` take an optional `YYYY-MM-DD` so a rendered cell is linkable (`/week/2026-10-16`). A day is drawn from **occurrences**, not definitions, so a repeating event appears on every day it falls and an instance the owner moved appears where they moved it; a week runs **Monday → Sunday** and a month is drawn as **whole weeks**, because a grid whose first row is four cells wide is a list, not a calendar. Each render is also **written** to `$DAEDALUS_HOME/ui/` and today's becomes the front page. The views read with `local_only` included: this page is the owner's own surface on the owner's own loopback with no model in the loop, and §6's withholding rule is about *egress* — hiding the owner's own appointment from the owner's own calendar would be the rule turned inside out.

**Mail — read-only.** Gmail `readonly` scope; read + summarize only. **No send, no labels, no delete.** Sending is not in the system; it would be a separate, deliberately added grant.

---

## 6. Trust & Safety

- **Self-contained actions** (querying, memory, local computation) → **auto**.
- **Anything leaving the machine** → **explicit owner approval**.
- **Reach narrow.** Local work reads the minimum needed; the agent never sweeps the filesystem, the vault, or unrelated projects to assemble context — it asks first.
- **Personal data is out of bounds.** Passwords, `.env`, `docs/private`, the Obsidian `private/` folder, and health / financial / relationship / location data are never opened, read, logged, summarized, or transmitted.
- **Bytes stay home.** Nothing leaves the machine without approval: no secret or personal value in any prompt, request body, log, or message. **Structure travels; values stay local** (§3.6).
- Approvals surface as **Discord buttons** for actions, **on the artifact** for visuals (§3.6).
- Standing grants (bounded, scoped, revocable) preferred over approval spam; strong confirmation retained for purchases, sensitive disclosure, irreversible ops.
- **The tier is decided from the call, deterministically** — never by the model, and never by a model round-trip. A call is judged by the worst plausible outcome of what it actually does: reads state → auto; changes something → ask; destroys data or history → ask loudly; can take the machine with it, or reaches a path that is out of bounds → **refused outright, never offered as a choice**. A command that cannot be judged is not called safe.
- **Consent is narrow.** A standing grant is remembered only for an action that is bounded, repeatable, and not destructive — an approval covers what was approved, and nothing more. Irreversible ops are never covered by a standing grant, and a refusal is always reported to the owner: silently stopping makes *tried and was stopped* indistinguishable from *never tried*.
- **The process environment is part of "bytes stay home."** Commands the agent runs do not inherit credentials; what a command prints returns into the conversation and on to a model.

---

## 7. Cabinet (8 seats)

Durable identities over the one loop. Charters and memory namespaces are separate; processes are not.

| Seat | Owns |
| --- | --- |
| **Daedalus** | Coordinator. Priorities, the honest weekly audit, kills stale work out loud, guards sleep. |
| **Athena** | Knowledge & coding mastery — DSA, aptitude, competitive programming, interview prep. Learning strategy. Pairs with Odysseus. |
| **Odysseus** | Research & investigation — the research thread, job-market intel. Pairs with Athena. |
| **Calliope** | Creative voice — writing, blog, portfolio, personal brand. The distribution channel. |
| **Icarus** | Engineering & prototypes. Owns the OpenCode delegate. |
| **Heracles** | The body — training, nutrition, sleep. |
| **dave** | *(lowercase)* Laptop ops — downloads, files, remote access. |
| **Melinoë** | Security, verification, privacy, approvals. |

**Athena vs Icarus:** Athena is for getting *better* at coding; Icarus is for *building* things.

**Charters are files.** The eight charters are the starting point, not the source: each lives at
`$DAEDALUS_HOME/cabinet/seats/<key>.toml` (`name`, `charter`, and optionally `namespace`, `active`,
`traits`), written out on first load and read at startup, so editing one changes what that seat
answers from on the next turn. A file that is deleted falls back to the built-in rather than to
nothing, so "never mind" is a thing the owner can say by removing a file. A file the loader cannot
believe — bad TOML, no charter, a field it does not know — **stops the startup with the path and the
problem**, because the failure it would otherwise produce (the built-in quietly answering in place of
the charter the owner wrote) is one they would never notice. Exactly one seat is active; two charters
claiming the owner is refused, and none leaves the default in place.

**Each seat keeps its own memory.** A seat reads and writes
`memory/namespaces/<key>/facets/`; the key is the directory name, so it is validated
as one. The coordinator reads `memory/facets/` at the root — the namespace
`daedalus` — which is where every fact the owner has told Daedalus already lives, so
the default identity's memory keeps behaving exactly as it did before seats had
namespaces. A charter may point its seat at another namespace; Daedalus's own cannot
be moved, since that is the tree those facts are in.

**One seat is active, and activating is a file edit.** `active = true` on exactly one
charter is the identity a nameless turn runs as; the default stands until the owner sets
another. The flag lives in the file — `daedalus activate athena` writes the files, it holds
no state of its own — and switching writes *every* file whose flag changes, because
activating one seat means deactivating another and two charters claiming the owner is
refused at the next startup. A name nobody answers to is not an error worth stopping a turn
for: the request lands on the active seat.

**The coordinator routes, so the owner does not have to name a seat.** A message is
routed *before* the turn starts. An address the owner wrote themselves — `athena: …`,
`@athena …`, `to athena: …` — wins with no model consulted, and a leading word that is
not a seat is an ordinary sentence rather than a failed address. Otherwise one
**cheap-route** question asks which seat the message belongs to, with the **charters
themselves as the menu**: a second list of responsibilities living in code would drift
the day a charter was edited, and it would be the copy the model believed. The answer is
validated against the cabinet, so routing cannot invent an identity, and everything
unreadable — no answer, an unknown name, a provider that is down, a message too short to
be about anything — **falls back rather than guessing**: the seat already handling this
conversation, else the active one. Routing is a preference, never a gate. The turn then
runs as that seat from its first token — that seat's charter, that seat's memory
namespace, that seat's voice — and the reply **names whoever took it** whenever it is not
the identity the owner was already talking to. That last part is what makes a wrong route
correctable rather than mysterious; a route nobody can see is a route nobody can fix.

**Seats do not call each other — they leave messages.** A seat is not a process, so
there is no socket to lose a message on and no in-memory list to keep one in. What one seat
says to another is a typed message (`request / ack / partial / result / verify / failure /
cancel`) written to `cabinet/messages.db`, with `correlation_id` naming the exchange and
`causation_id` naming the message it answers. The queue is **at-least-once with dedup, never
exactly-once**: a message is offered until it is resolved, and each offer is counted, so a
seat handed a message a second time can see that whoever held it before may have half-done
the work. What makes the repeat safe is that the sender chose the message's id **before** its
first attempt — a retry carrying the same id is the same message, and the second answer to a
resolved message is recognised as the duplicate of the first.

**A scheduled job can belong to a seat (T21).** A scheduler job may name its seat; a
seated wake composes from *that* seat's charter, writes into *that* seat's memory
namespace, and delivers under its name — so a Monday sweep arrives as
`Odysseus: …`, not as the coordinator's voice which the owner never asked to hear
in that register. A job whose seat answers to nobody is recorded as a failed run,
never quietly delivered as some other identity; `schedule op=add` refuses such a
seat at the door. Only a wake can be seated: a capability job runs headless and
speaks through its own manifest, and a one-shot `remind` is never seat-attributed —
it is the owner's alarm, not anyone's work. Jobs remain file-first, and a store
that only read the file at startup would make hand-editing a lie, so the next tick
re-reads `jobs.toml` — an added job becomes real without a restart.

---

## 8. Runtime & Process

- **systemd `--user` units**, `loginctl enable-linger`, journald logs.
- Laptop **stays awake with the lid closed** (required for remote access and overnight work).
- No always-on heavyweight daemon; lazily launched, suspend-aware.

---

## 8.5. Setup wizard

An interactive, **re-runnable** first-run wizard that provisions the system end to end and **refuses to declare success until it verifies**. It is the **owner-driven provisioning surface** (§3.5) — the one place secrets and access are granted.

**Form.** A committed bash wizard built on the standard `template.sh` library (stage-by-stage progress, URL opening, hidden secret entry, idempotent `.env` upserts, confirmation gates). It calls into the product (`daedalus …`) for anything the product owns — the self-test and unit install.

**Stages (order):**

1. **Preflight** — Python 3.14 / uv, systemd `--user`, tailscale, RAM / disk; report, and stop on a hard miss.
2. **Layout** — create `$DAEDALUS_HOME` at `0700`; write `config.toml`.
3. **Secrets** — initialise the encrypted store; generate the master key; enforce `0600`. Nothing is ever echoed.
4. **Owner identity** — Discord user id(s) → the DM allowlist; DM-only confirmed.
5. **Models** — NIM + Z.ai keys; probe each; write the fallback chain.
6. **Local runner** — install / pull Bonsai 8B; verify.
7. **Discord** — create app + bot, capture the token, invite; verify with a round-trip DM to the owner.
8. **Resources (optional, skippable)** — `gcal` (read-all / write-own), mail (`readonly`), **Tailscale** (install and login asserted, **Funnel asserted off**, and an API key captured into the secret store so tailnet hygiene can be *proposed* for approval rather than remaining a console action); the wizard opens the URL and the owner completes each grant.
9. **Trust posture** — default tiers (auto / ask / block); standing grants (the `Daedalus` calendar, egress); quiet hours.
10. **Runtime** — write systemd `--user` units; `enable-linger`; lid-closed `logind`; enable timers.
11. **Hardening** — assert perms `0700` / `0600`, **no plaintext secrets at rest**, dashboard loopback + **Tailscale Serve (no Funnel)** with tailnet auth, DM allowlist on, egress gates on, quarantine on.
12. **Verification — the mandatory end** — run `daedalus selftest`: model probe · Discord round-trip · memory write/read · capability author + **Melinoë gate** · scheduler tick · **loopback serve** (private bind, writes gated) · dashboard render · calendar sync (if configured). A **green checklist, or it fails loudly** — the wizard never reports success with a red item.
13. **Handoff** — print the first message to send it.

**Properties.** Idempotent and re-runnable (repair mode); secrets only via hidden entry, written to the encrypted store / `.env`; `confirm` before any irreversible action; the resource stages are individually re-runnable so later provisioning reuses them.

---

## 9. Surface (Discord)

- **One bot.** **DM-first.** **Free text** — no slash-command vocabulary required.
- **Buttons only for approvals.**
- Long tasks run in threads.
- The "daily message" is **not a template** — it is the agent *choosing* to speak, composed fresh each time. The scheduler triggers; the model writes.
- **Whether it may speak is decided in code, not by the model** (§6). *Quiet hours* silence a wake outright, and a *daily cap* bounds how often it may open its mouth, so a cadence job cannot become a drip. Within those bounds the model still owns *substance*: it may look at the day and reply `[silent]`. A notification from an authored capability is **not** a wake and is **not** gated — the owner defined that capability and asked it to speak up, and silencing an alert at 03:00 would defeat the thing it was built for.
- **The turn's work is visible while it happens** — one live status message per turn
  (tool, op, outcome; never the owner's words, never the arguments), edited in place
  under Discord's edit limit, and deleted when the turn ends so the answer stands
  alone. The one exception is a turn the **engine stopped** — a repeated failure or
  the step limit — whose ledger is held by the surface, because the reply summarizes
  one failing call and the ledger shows all of them (T44).
- Errors travel to the owner **fenced** (T44): raw tool error text goes out in a code
  block, because Discord's markdown italicises pairs of `*` and rewrites things like
  a five-field cron example on the way out.

---

## 10. Phases

- **P0 — the loop** *(delivered: loop, primitives, capability substrate, memory v0, scheduler v0, Melinoë gate, Discord surface, setup wizard)*
- **P1 — memory** *(delivered: file-first source of truth, supersession + bitemporal, `local_only` egress gate, Obsidian-compatible notes, index rebuildable from the notes)*
- **The internal calendar** *(delivered ahead of P2: file-first `.ics` + a derived index, served by the `calendar` primitive; `gcal` still unbuilt)*
- P2 — proactive scheduler *(delivered: quiet hours and a daily cap enforced in the scheduler, a wake briefing assembled from local signals, and the agent free to choose silence)*
- P3 — seats wired **one at a time, each as it earns its place**
- P4 — remote / Tailscale Serve
- P5 — OpenCode delegation
- P6 — the group chat (last)
- **Mail (read-only) — deliberately last, after everything above.** Cut from the
  calendar/integrations bundle by the owner: the calendar is the useful half, and a
  read-only mail scope that is never exercised is a standing risk taken for nothing.

---

## 11. P0 — Definition & Build Order

**Definition.** The general free-text loop **plus the capability substrate (§3.5)**. Proof-of-life is **not** a hardcoded feature — it is the owner sending a free-text request it has never seen and watching it complete end-to-end (author a capability, run it, schedule it, remember, produce output) **without touching code**.

**Why the substrate is in P0:** the proof-of-life *cannot happen* without Layer 1 (primitives) and Layer 2 (authored capabilities). They are not a later phase.

**Constraint (accepted).** The P0 loop ships and does one real thing before any cabinet seat is wired.

**Satisfied — 17 Oct 2026.** Seen in the journal, not taken on faith: an unseen free-text request at 08:24 authored a capability (`capability op=list` → `capability` → `schedule`), and a later one answered a calendar question with `calendar op=agenda -> ok` — no code touched, no hardcoded flow. The gate on §7's seats is therefore met; what keeps a seat out of the delivered default now is only phase order.

**Build order:**

1. **Scaffold** — uv project, Python 3.14, package layout, config, `.env` (reuse existing creds: `NVIDIA_API_KEY`, `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS`), journald logging.
2. **Model client** — one OpenAI-compatible client behind a single `base_url`; NIM chain + fallback; Bonsai 8B local; cross-process rate-limit ledger.
3. **Primitives (Layer 1 + capability tool)** — `shell`, `code`, `files`, `http`, `browser`, `search`, `schedule`, `remind`, `process`, `memory`, `discord`, `ask`, `secrets`, plus `capability` (Layer 2 author/run/verify/schedule/remove).
4. **Approval middleware** — trust tiers; egress needs one-time approval → standing grant; buttons.
5. **Discord surface** — one bot, DM allowlist (owner only), free-text handler, approval buttons.
6. **The loop** — message → context assembly → model → multi-step tool calls → streamed response.
7. **Capability substrate (Layer 2)** — registry + `capabilities/<name>/{run.py, manifest.toml, README.md, state/}`; per-capability uv venv; startup inventory injected into context; MCP seam noted (§3.5).
8. **Memory v0** — file-first store (markdown + SQLite index); write a fact, recall a fact.
9. **Scheduler v0** — cron-pumped wake to run authored capabilities (no model) and to let the agent speak first. A moment that falls while the laptop is asleep is not lost: the tick that follows catches it up — **once**, staggered, and only while it is still true (ARCHITECTURE §7.3.3).
10. **Melinoë verification gate** — a newly authored capability is run once and checked before it is scheduled.
11. **Proof-of-life** — the unseen free-text test.

**Acceptance:** the free-text test passes; nothing in the loop is a hardcoded flow.

**Setup path:** `scripts/setup.sh` (§8.5) provisions, hardens, and verifies a fresh install — including the `daedalus selftest` that proves the loop. It is part of the P0 deliverable.

---

## 12. Reconciliation notes

**Adopted (diverging from `daedalus.md`):**
- Custom harness instead of extending Hermes.
- NIM (GLM chain) + Bonsai 8B local instead of Hermes/OpenRouter/Nemotron.
- Cabinet: **8 seats**.

**Hard-blocked, always:** password files, `docs/private`, the Obsidian `private/` folder, `.env`, and any health / financial / relationship data — high-trust scope, never exported, never on Discord.

---

## 13. Open implementation details (not design decisions)

- Exact local runner for Bonsai 8B 1-bit weights (Ollama vs llama.cpp/PrismML fork).
- **Embedding choice for memory recall — resolved (P1).** Recall is **SQLite FTS5**
  over the facet markdown, which is the source of truth and rebuilds the index at
  will. Local embeddings stay a seam behind `MemoryStore.recall()`, to be added only
  if recall quality actually demands it; nothing else in P1 depends on the choice.
- **Scheduler transport — resolved.** The daemon pumps its own ticks in-process
  (`daedalus run`): there is deliberately no separate `tick` verb, so the systemd
  timer triggers `daedalus run` rather than a one-shot.
- **Proactive cadence — resolved: none by default.** The gate (quiet hours, daily cap) and
  the composition path are built, but **no wake job ships**. The agent speaks first only
  once the owner asks for a cadence, and the cron expression is the owner's to choose. An
  agent that starts talking before it has been asked to is a decision, not an
  implementation detail — and that decision has not been made (owner, 2026-10-16).
- Capability `manifest.toml` schema (scope, deps, schedule, io) and how the model invokes a named capability (registry lookup vs. generated tool-call).
- Resource provisioning flow when a resource is added later (e.g. mail OAuth) — one-time local setup, never on Discord.
- **Headless-browser choice — resolved.** Playwright, with its bundled headless Chromium;
  `browser` returns an actionable error when it is not installed, so the primitive
  degrades instead of failing obscurely. Chromium's footprint is paid only while a page
  is open.
- Resource-registry schema — how a resource declares access + credentials.
- Generative-UI spec schema + component-registry (component + token) format.
- Mood → token mapping details (source resolved: inferred by default from **structured signals**, one-word override; message tone read only by a **local** model).
- Regeneration trigger (on view vs on schedule vs on context change).
- Tuner control set — which token and system dimensions are directly adjustable on the dashboard.
- Dashboard interactions that are *actions* (not tuning) — routed through the Discord approval split.
- Long-running-process supervision, *beyond* what ships — a started process is now
  recorded durably (its pid **and its `/proc` start time**, so a recycled pid is
  never mistaken for it), stays listed and stoppable across a daemon restart, and
  its ending is reported with a time and an exit code instead of a bare `rc=None`.
  What is still a call, not an implementation detail: a clean shutdown still stops
  what the daemon started, and nothing restarts a process that died on its own.
- Capability resource caps beyond what ships — `time_s`, `cpu` and `mem_mb` are all
  enforced now (see ARCHITECTURE §11), but enforcement is per-process: a **cgroup**
  would bound a capability's whole *tree* and could express a sub-core share, which
  affinity and resident sampling cannot.
- gcal sync details — cursor/token storage, tombstone and delete semantics, conflict edge cases across the authority split.
- Calendar dashboard scope — which views (day/week/month) and which edit operations ship first.
- Mail summarize cadence — on-demand vs scheduled digest.
