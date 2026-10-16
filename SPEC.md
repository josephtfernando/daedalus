# Daedalus — Spec

**Scope:** single-user, local-first (only the owner can talk to it)
**Status:** P0 and P1 implemented as code (loop + primitives + capability substrate + memory depth + internal calendar + scheduler + proactive gate + Melinoë gate + Discord surface + setup wizard), 190 tests green; `0.1.0` pending Discord proof-of-life
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

The cabinet, theming, remote/Tailscale access, OpenCode delegation, and the multi-agent group chat are **out of scope** until the loop does a real thing that was not hardcoded.

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
- **`serve`** — spin up a loopback HTTP server, published privately via **Tailscale Serve** (loopback-bound, **no public Funnel**). This is how a served project, a dashboard, or the **calendar view** reaches the phone.
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
- **Context signals — all in-bounds:** time / day / season, calendar, active task / thread, recent agent activity, battery / AC, focus / quiet hours, weather, body data, financial / application pipeline.
- **Structure remote, values local.** The model composes **structure** — which components, which layout, which keys — never the sensitive **values**. Data binding happens **locally** in the renderer, so `local_only` values never enter a remote prompt. A dashboard that inherently needs local-only values *in the prompt* is composed by a **local model**. This is the §6 sink policy applied to the UI.
- **Output** — self-contained HTML; served loopback + **Tailscale Serve** (`serve`), pushable via **Taildrop** (`push`) — §3.5 output channels.

### Approval & tuning surface

- **Approval-by-rendering.** Visual artifacts — themes, components, dashboards, and the dashboard itself — are approved **on the served artifact**, not in chat: the proposal renders **live**, the owner tunes it in place, and approval is a control on that page. *Seeing is the most accurate way to say yes.* Action approvals (egress, purchases, irreversible ops) stay as **Discord buttons** (§6). The split is: **visual → dashboard, action → Discord.**
- **Control surface / tuners.** The dashboard is interactive; controls call back into the agent. **Tuners** adjust the live theme (palette / density / motion / type) and, by extension, **system knobs** (schedules, thresholds, quiet hours, seat behaviour). Tuned values form a **pending** set; **approve** adopts them into the owner-approved store. Drafts, approvals, and the token store persist locally.
- **Security.** Served over the tailnet, single-user, tailnet-authenticated. Write actions (tune / approve) are gated like any other action — no anonymous controls.

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
  - **projects + status**
  - **research thread**
  - **published posts / brand**

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
replaces exactly one instance. `agenda` and every other read expand the rule and answer in
dated occurrences, so "every weekday at 9:10" answers for the day being asked about rather than
existing as a rule nobody can read. Writes say what they mean: `scope=all` edits the series,
`scope=this` the one instance, `scope=future` splits the series at that point. A `VALARM` is
kept as an offset from the start, and `TRANSP` records whether an event makes the owner busy.
Driving gcal itself is deferred: the owner's calendar is the one on this laptop.

**`gcal` — a resource, read all / write one.**

- **Read** every calendar the owner has — context for the day.
- **Write** only a dedicated **`Daedalus`** calendar it owns — never a personal calendar.

**Authority split — no heuristics:**

- `Daedalus` calendar → **internal-authoritative**; gcal mirrors it.
- Every other calendar → **gcal-authoritative**; the internal copy is a read cache.

**Sync.** Cursor/token-based: pull changes since the last sync, push local changes; **tombstones** for deletes so a sync cannot resurrect them. Triggered on wake and on a cadence. Writing is an **egress action → standing grant**, scoped to the `Daedalus` calendar.

**Dashboard.** The internal calendar has a **full dashboard view** (`serve`) — day / week / month, create / edit / delete — used the way gcal is used. Controls route through the approval split (§3.6).

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
8. **Resources (optional, skippable)** — `gcal` (read-all / write-own), mail (`readonly`), tailscale; the wizard opens the URL and the owner completes each OAuth.
9. **Trust posture** — default tiers (auto / ask / block); standing grants (the `Daedalus` calendar, egress); quiet hours.
10. **Runtime** — write systemd `--user` units; `enable-linger`; lid-closed `logind`; enable timers.
11. **Hardening** — assert perms `0700` / `0600`, **no plaintext secrets at rest**, dashboard loopback + **Tailscale Serve (no Funnel)** with tailnet auth, DM allowlist on, egress gates on, quarantine on.
12. **Verification — the mandatory end** — run `daedalus selftest`: model probe · Discord round-trip · memory write/read · capability author + **Melinoë gate** · scheduler tick · dashboard render · calendar sync (if configured). A **green checklist, or it fails loudly** — the wizard never reports success with a red item.
13. **Handoff** — print the first message to send it.

**Properties.** Idempotent and re-runnable (repair mode); secrets only via hidden entry, written to the encrypted store / `.env`; `confirm` before any irreversible action; the resource stages are individually re-runnable so later provisioning reuses them.

---

## 9. Surface (Discord)

- **One bot.** **DM-first.** **Free text** — no slash-command vocabulary required.
- **Buttons only for approvals.**
- Long tasks run in threads.
- The "daily message" is **not a template** — it is the agent *choosing* to speak, composed fresh each time. The scheduler triggers; the model writes.
- **Whether it may speak is decided in code, not by the model** (§6). *Quiet hours* silence a wake outright, and a *daily cap* bounds how often it may open its mouth, so a cadence job cannot become a drip. Within those bounds the model still owns *substance*: it may look at the day and reply `[silent]`. A notification from an authored capability is **not** a wake and is **not** gated — the owner defined that capability and asked it to speak up, and silencing an alert at 03:00 would defeat the thing it was built for.

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

**Build order:**

1. **Scaffold** — uv project, Python 3.14, package layout, config, `.env` (reuse existing creds: `NVIDIA_API_KEY`, `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS`), journald logging.
2. **Model client** — one OpenAI-compatible client behind a single `base_url`; NIM chain + fallback; Bonsai 8B local; cross-process rate-limit ledger.
3. **Primitives (Layer 1 + capability tool)** — `shell`, `code`, `files`, `http`, `browser`, `search`, `schedule`, `process`, `memory`, `discord`, `ask`, `secrets`, plus `capability` (Layer 2 author/run/verify/schedule/remove).
4. **Approval middleware** — trust tiers; egress needs one-time approval → standing grant; buttons.
5. **Discord surface** — one bot, DM allowlist (owner only), free-text handler, approval buttons.
6. **The loop** — message → context assembly → model → multi-step tool calls → streamed response.
7. **Capability substrate (Layer 2)** — registry + `capabilities/<name>/{run.py, manifest.toml, README.md, state/}`; per-capability uv venv; startup inventory injected into context; MCP seam noted (§3.5).
8. **Memory v0** — file-first store (markdown + SQLite index); write a fact, recall a fact.
9. **Scheduler v0** — cron-pumped wake to run authored capabilities (no model) and to let the agent speak first.
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
- Long-running-process supervision (how `process` children survive restarts / sleep).
- Capability resource-cap enforcement — `time_s` is enforced as a hard timeout, but
  `cpu` / `mem_mb` are declared in the manifest and not yet applied to the child. A
  rushed `RLIMIT_AS` can kill a healthy Python process, so this wants the rlimit-vs-cgroup
  choice made deliberately rather than guessed.
- gcal sync details — cursor/token storage, tombstone and delete semantics, conflict edge cases across the authority split.
- Calendar dashboard scope — which views (day/week/month) and which edit operations ship first.
- Mail summarize cadence — on-demand vs scheduled digest.
