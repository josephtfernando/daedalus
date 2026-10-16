# Tickets

The working backlog: what is being built next, in what order, and what "done"
means for each. `SPEC.md` remains the **source of truth for the design** — if a
ticket and the spec disagree, the spec wins and the ticket is wrong. This file is
the *execution order*, and it is expected to be edited as the order changes.

Status: `open` · `doing` · `done`. A ticket is `done` only when its acceptance
line is satisfied by a test or a selftest check, the docs and `CHANGELOG.md` are
updated, and it is committed on its own.

---

## Three workstreams

| Stream | Why now |
| --- | --- |
| **A — Serving** | `genui/server.py` is on the critical path for *both* the dashboard and P4 (ARCHITECTURE §569). Nothing can be published to the phone until something is served. |
| **B — Dashboard** | `SPEC.md` §5.5 promises the calendar a **full dashboard view**; `SPEC.md` §8.5 step 12 makes "dashboard render" part of the mandatory selftest. Neither exists — `genui/` is entirely unbuilt. |
| **C — Seats (P3)** | The stated next phase, and the smallest of the three. The loop already threads `seat=`; what is missing is charts-as-files, memory namespaces, and a way to activate one. |

**Dependencies:** `T1 → T2, T6`; `T3 → T4 → T6 → T7 → {T8, T9}`; `T6 → T10`;
`T11 → T12 → T13 → T14`. Stream C does not touch Stream B; either may be done first.

**Order:** T1 · T3 · T4 · T5 · T6 · T10 · T7 · T11 · T12 · T13 · T8 · T9 · T2 · T14 · T15

---

## Stream A — serving

### T1 — `genui/server.py`: the loopback server, and `daedalus serve` · `done`
*SPEC §3.5 (`serve`), §3.6 (output), ARCHITECTURE §7.4, §9.5.*

An HTTP server bound to **127.0.0.1 and nothing else**, serving the generated
artifacts in `$DAEDALUS_HOME/ui/`, with the control API from ARCHITECTURE §9.5
written down as a routing table and a **per-install token** gating every write.
Never `0.0.0.0`, never a bind the owner did not ask for.

**Acceptance:** `daedalus serve` answers on loopback; the address the socket
actually came up on is asserted to be a loopback address; a `POST` without the
token is refused `401`, with the token it gets *past the gate* (a `501` from the
unbuilt route, not a `401`); an unset token accepts no writes at all; a path
escaping `ui/` is refused; `daedalus selftest` gains an eleventh check proving all
of it. **Done** — 32 tests in `tests/test_serve.py`.

### T2 — Publish over Tailscale Serve, never Funnel · `open`
*SPEC §3.5, §8.5 step 11 ("dashboard loopback + Tailscale Serve (no Funnel) with tailnet auth").*

`daedalus serve --publish` runs `tailscale serve --bg http://127.0.0.1:<port>`
and prints the command **before** running it. Refuses when `tailscale` is absent
or when Funnel is enabled — the loopback bind is the inner gate, the tailnet the
outer one, and a public Funnel would remove the outer gate entirely.

**Acceptance:** a missing `tailscale` is a clear refusal, not a traceback; no
code path enables `funnel`; the tailnet URL is printed for the owner to open.

## Stream B — the dashboard

### T3 — `genui/spec.py`: the UI spec schema · `open`
*SPEC §3.6 ("the model proposes a UI spec; a deterministic renderer disposes"), §13 (spec schema, open).*

A declarative spec — an ordered list of components with typed props and a layout
— validated against the component registry. The model composes *structure*; it
never emits markup. Unknown component, unknown prop, or a prop carrying markup →
refused with an actionable message.

**Acceptance:** a valid spec round-trips through JSON; an unknown component type
is refused naming it; a prop containing `<script>` is refused or escaped, and a
test says which.

### T4 — `genui/components/`: the first component vocabulary · `open`
*SPEC §3.6 (component list), ARCHITECTURE §133.*

`stat-card`, `list`, `timeline`, `table`, `prose`, and `calendar` — the set the
calendar view actually needs, each with a prop schema and an HTML renderer.
Components are documented as *authored capabilities* in the spec's long run
(§3.6); for now they are a built-in registry with the same validated shape.

**Acceptance:** every registered component renders from valid props and refuses
missing required ones; the registry is enumerable for the prompt so the model
knows the vocabulary.

### T5 — `genui/theming.py`: tokens, and mood → token set · `open`
*SPEC §3.6; §13 (mood→token mapping, open).*

Validated token sets (palette / type / density / motion) for the starting moods
`calm`, `midnight-focus`, `dense-ops`, `low-energy`, `celebration`. Mood is
inferred from structured signals, with a one-word owner override; an unknown mood
falls back to `calm` rather than rendering unstyled.

**Acceptance:** every mood yields a *complete* token set (missing a token is a
failure, not a default); the override beats inference; unknown mood → `calm`.

### T6 — `genui/renderer.py`: spec + local values + tokens → HTML · `open`
*SPEC §3.6 ("structure remote, values local"), ARCHITECTURE §7.4, §254.*

Bind values **locally**, apply the mood token set, emit one self-contained HTML
document with no external fetches. A `local_only` value must never have entered
the spec the model saw — the renderer is where structure becomes the owner's data.

**Acceptance:** the rendered page is self-contained (no `http://`/`https://` asset
references) and every interpolated value is escaped; a `local_only` event appears
on the page while being absent from any model-visible spec, proven together.

### T10 — Selftest: dashboard render · `open`
*SPEC §8.5 step 12 ("dashboard render" is part of the mandatory end-to-end verification).*

The selftest has ten checks and none of them renders anything, though the spec
lists it among the things that must be green.

**Acceptance:** a check renders a dashboard from a fixed context and asserts the
HTML is self-contained and contains the calendar value it was given.

### T7 — The calendar dashboard: day / week / month · `open`
*SPEC §5.5 line 317.*

Read the internal store and render three views. Occurrences, not definitions —
the same rule every other read follows (`SPEC.md` §5.5).

**Acceptance:** each view renders the right events for a fixed clock; a repeating
event appears on each day it falls; the page is written to `ui/` and served by T1.

### T8 — Dashboard controls → the approval split · `open`
*SPEC §3.6 ("visual → dashboard, action → Discord").*

Create / edit / delete from the page `POST /action`s into the **same** approval
middleware the tool uses. A control that needs approval surfaces a Discord button
and the page says it is waiting.

**Acceptance:** a delete from the dashboard is refused without the authorization
the `calendar` tool requires — a test drives both paths and compares.

### T9 — `genui/tuners.py`: pending → approved · `open`
*SPEC §3.6 (control surface), ARCHITECTURE §9.5 (`POST /tune`, `POST /approve`).*

A tuned value forms a **pending** set, visible in a preview, adopted only on
approve. Tokens and drafts persist under `genui/`.

**Acceptance:** a tune does not change the live theme; approve does; the pending
set survives a restart.

## Stream C — seats (P3)

### T11 — Charters become files: `cabinet/seats/*.toml` · `open`
*SPEC §7, ARCHITECTURE §132, §149, §554.*

The eight identities exist in `cabinet/seat.py` as literals. Move the charters to
files the owner can edit, and load them at startup — file-first, like memory and
the calendar. A malformed file is reported, never swallowed.

**Acceptance:** editing a charter changes the system prompt; a broken file is an
actionable error; the built-in charters remain the fallback for a fresh install.

### T12 — `cabinet/namespace.py`: seat memory namespaces · `open`
*ARCHITECTURE §12 ("identity + charter + memory namespace"). `Seat.namespace` exists and is used nowhere.*

Scope memory reads and writes by seat. Daedalus's own namespace must keep
behaving exactly as it does today, or every existing fact is orphaned.

**Acceptance:** a fact written under seat A is not recalled under seat B; a fact
written before this ticket is still recalled by Daedalus afterwards.

### T13 — Activate one seat · `open`
*SPEC §7 (no seat is wired until the proof-of-life holds), AGENTS.md.*

Choose a seat for a turn, put its charter in the prompt, and keep **one** active
at a time. The default stays Daedalus.

**Acceptance:** activating a seat changes the identity and charter the model
sees; exactly one seat is active; the default is unchanged when nothing is set.

### T14 — `cabinet/protocol.py` + `broker.py`: seat↔seat · `open`
*ARCHITECTURE §9.2, §7.5. Only needed once two seats must actually collaborate.*

The typed message shape, `cabinet/messages.db`, at-least-once delivery **with
dedup** (never exactly-once), correlation and causation links.

**Acceptance:** a message survives a restart; a redelivered message is deduped; a
reply carries the id of the request it answers.

### T15 — P4: reach it from the phone · `open`
*SPEC §10 (P4), §3.5 (`push` / Taildrop).*

What T2 leaves: the dashboard over the tailnet, and the `push` output channel.

**Acceptance:** the dashboard opens from the phone; the tailnet is the only way
in; a `push` delivers a file over Taildrop.
