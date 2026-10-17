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

**Order:** T1 · T3 · T4 · T5 · T6 · T10 · T7 · T11 · T12 · T13 · T8 · T9 · T2 · T14 · T18 · T19 · T20 · T15

---

## Found while working

Fixes that belong to no stream, recorded so the plan is the whole story.

### T17 — A series is several entries, and a change must move all of them · `done`
*`calendar/store.py` (`_move`, `_shift_exceptions`, `_update_future`). Found while testing the shift.*

`shift` moved a repeating event's rule and nothing else. Everything the rule *excepts* — a `VEVENT`
per instance the owner edited, an `EXDATE` per skipped day, an `RDATE` per added day, the `UNTIL`
inside the rule — stayed on the old dates, so the skipped days came back and the edited instances
were stranded. A `scope="future"` split had the sibling fault: the overrides it inherited kept
pointing at the master, and the tail produced its own occurrence on the same day, so one moved
standup read as two.

**Acceptance:** shifting a series moves its exceptions by the same delta and the count of what is on
does not change (beyond the one the window gains at its far edge, because a weekly rule has no
beginning to run out of); an `UNTIL` moves with its rule and a `COUNT` does not need to; a split
gives its overrides to the tail. **Done** — `_move` takes the delta before the rest of the change and
carries it through `_shift_exceptions`; `_update_future` re-points the inherited `VEVENT`s. Sixteen
tests, including a round trip that shifts a series a week back and forward.

### T16 — A pinned tick was judged against the wall clock · `done`
*`scheduler/scheduler.py`, `scheduler/jobs.py`. Found at 12:01.*

The selftest's proactive probe drives a pinned sequence — a silent 03:00, then a
noon that must speak — and each run recorded the job's `last_run` from the **wall
clock** while judging dueness against the pinned time. The probe passed all
morning and failed from 12:01, making `daedalus selftest` report 10/11 for the
rest of the day.

**Acceptance:** a tick given a time is governed by that time; the same pinned
minute still does not run twice. **Done** — `dispatch_tick` records
`when.timestamp()`, with two tests that pin times in the past so a wall-clock
implementation fails whenever the suite runs rather than only after noon. The
proactive probe has never ticked the same job twice at pinned hours before now,
which is why this was invisible until the clock moved.

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

### T2 — Publish over Tailscale Serve, never Funnel · `done`
*SPEC §3.5, §8.5 step 11 ("dashboard loopback + Tailscale Serve (no Funnel) with tailnet auth").*

`daedalus serve --publish` runs `tailscale serve --bg http://127.0.0.1:<port>`
and prints the command **before** running it. Refuses when `tailscale` is absent
or when Funnel is enabled — the loopback bind is the inner gate, the tailnet the
outer one, and a public Funnel would remove the outer gate entirely.

**Acceptance:** a missing `tailscale` is a clear refusal, not a traceback; no
code path enables `funnel`; the tailnet URL is printed for the owner to open.
**Done** — `genui/publish.py`: the command is printed before it runs, the URL is
read back from `tailscale serve status`, and Funnel-on is read from
`serve status --json` and refused by name with the way out. Subprocesses run
with stdin cut off, so an unauthenticated CLI that stops to ask hangs nothing.
On ctrl-c the route is taken back down — a published route pointing at a dead
port is a broken dashboard on the phone. 14 tests in `tests/test_publish.py`,
including the acceptance and the "no code path enables Funnel" property (every
command built is a `serve` or a `status`; the word is only ever read).

## Stream B — the dashboard

### T3 — `genui/spec.py`: the UI spec schema · `done`
*SPEC §3.6 ("the model proposes a UI spec; a deterministic renderer disposes"), §13 (spec schema, open).*

A declarative spec — an ordered list of blocks with typed props — validated
against the component vocabulary. The model composes *structure*; it never emits
markup. Unknown component, unknown prop, missing required prop, wrong type, or a
value carrying markup → refused with a message that names the way out.

**Acceptance:** a valid spec round-trips through JSON; an unknown component type
is refused naming it *and* listing the vocabulary; a prop containing `<script>` is
refused at the door (and escaped again at render — that second defence is T6).
**Done** — 38 tests in `tests/test_genui_spec.py`.

### T4 — `genui/components/`: the first component vocabulary · `done`
*SPEC §3.6 (component list), ARCHITECTURE §133.*

`stat-card`, `list`, `timeline`, `table`, `prose`, and `calendar` — the set the
calendar view actually needs, each with a prop schema and an HTML renderer.
Components are documented as *authored capabilities* in the spec's long run
(§3.6); for now they are a built-in registry with the same validated shape.
Components carry no styling — they emit semantic class names and the mood token
set supplies the CSS.

**Acceptance:** every registered component renders from valid props and refuses
missing required ones; the registry is enumerable for the prompt so the model
knows the vocabulary. **Done** — 35 tests in `tests/test_genui_components.py`,
including the escape proved with the validator deliberately bypassed. T4 also
added `Prop.choices` and `Component.check` to `spec.py`.

### T5 — `genui/theming.py`: tokens, and mood → token set · `done`
*SPEC §3.6; §13 (mood→token mapping, open).*

Validated token sets (palette / type / density / motion) for the starting moods
`calm`, `midnight-focus`, `dense-ops`, `low-energy`, `celebration`, plus the one
place CSS is written (the component sheet, reading only declared tokens). Mood is
inferred from structured local signals, with a one-word owner override that wins;
an unknown mood falls back to `calm` rather than rendering unstyled.

**Acceptance:** every mood yields a *complete* token set (missing a token is a
failure, not a default); the override beats inference; unknown mood → `calm`.
**Done** — 46 tests in `tests/test_genui_theming.py`. A token is validated, not
free-form: hex colours, units on lengths, and a font stack that cannot contain a
parenthesis (so `url(…)` cannot ride in).

### T6 — `genui/renderer.py`: spec + local values + tokens → HTML · `done`
*SPEC §3.6 ("structure remote, values local"), ARCHITECTURE §7.4.*

Bind values **locally** through `Spec.bind()`, apply the mood token set, emit one
self-contained HTML document with no external fetches and a
`Content-Security-Policy` of `default-src 'none'`. A `local_only` value reaches
the page without ever having been in the spec the model saw — *structural*, not
filtered.

**Acceptance:** the rendered page is self-contained (no `http://`/`https://`, no
`url(`, no script or link) and every interpolated value is escaped; a `local_only`
event appears on the page while being absent from any model-visible spec, proven
together, before and after rendering. **Done** — 29 tests in
`tests/test_genui_render.py`.

### T10 — Selftest: dashboard render · `done`
*SPEC §8.5 step 12 ("dashboard render" is part of the mandatory end-to-end verification).*

The twelfth check renders a day view from the scratch calendar: an event written,
read back as an occurrence, bound into a `calendar` block, and required to appear
on a page that names no remote origin and never had the value in the
model-visible spec.

**Acceptance:** a check renders a dashboard and asserts the HTML is self-contained
and contains the calendar value it was given. **Done** — `_check_dashboard`, with
two new renderer tests for what it exposed: `Component.check` was running before a
binding resolved, so it is now deferred to the pass that has the values.

### T7 — The calendar dashboard: day / week / month · `done`
*SPEC §5.5 line 317.*

`genui/dashboard.py` reads the store and returns a page (spec + values + mood);
`genui/server.py` answers `/dashboard`, `/day/`, `/week/`, `/month/`, each
optionally pinned to a date. Occurrences, not definitions; Monday → Sunday; a
month in whole weeks; the spec carries keys and the values are bound locally.

**Acceptance:** each view renders the right events for a fixed clock; a repeating
event appears on each day it falls; the page is written to `ui/` and served by T1.
**Done** — `tests/test_dashboard.py` (33 tests, including a live-socket render),
verified end-to-end against `daedalus serve` in a scratch home. The one thing it
was not built with: the *generative* dashboard (T9) and the controls (T8).

### T8 — Dashboard controls → the approval split · `done`
*SPEC §3.6 ("visual → dashboard, action → Discord").*

Create / edit / delete from the page `POST /action`s into the **same** approval
middleware the tool uses. A control that needs approval surfaces a Discord button
and the page says it is waiting.

**Acceptance:** a delete from the dashboard is refused without the authorization
the `calendar` tool requires — a test drives both paths and compares.
**Done** — `genui/actions.py` is the control surface: a closed vocabulary
(`calendar.create|update|delete`), resolved to a real tool call and put through
`approvals.authorize` with the tool's own risk, scopes and assessment. `allow`
runs it on the daemon's loop; `ask` schedules the Discord button and answers
*"waiting"* at once; `blocked` is refused with the policy's reason. The strip on
the page is built by code, outside the spec, so a model-authored page carries no
actions; it is served only to a verified session (one bootstrap URL → an
`HttpOnly` cookie) under `form-action 'self'`, while every model-composed page
keeps `form-action 'none'`. 34 tests in `tests/test_actions.py`, including the
acceptance: the same call driven through the tool's path and the page's, verdicts
compared — including a block-tier tool both paths refuse. Writing it exposed a
calendar bug (fixed below): naming one instance while leaving `scope` unset
silently acted on the **whole series**.

### T9 — `genui/tuners.py`: pending → approved · `done`
*SPEC §3.6 (control surface), ARCHITECTURE §9.5 (`POST /tune`, `POST /approve`).*

A tuned value forms a **pending** set, visible in a preview, adopted only on
approve. Tokens and drafts persist under `genui/`.

**Acceptance:** a tune does not change the live theme; approve does; the pending
set survives a restart.
**Done** — `Tuners` keeps drafts as files (`genui/drafts/<id>.toml`) and adopts
into `genui/tokens/approved.toml`, the only file the renderer reads; `resolve`
lays the adopted values **over** whichever mood is chosen. The preview is the real
dashboard at `/draft/<id>`, with Adopt/Discard under it when the request carries a
session. Values are validated at the tune and again at adoption. 35 tests in
`tests/test_tuners.py`; the selftest gained a 13th check (tuned → pending →
adopted, live theme moving only on the yes).

## Stream C — seats (P3)

### T11 — Charters become files: `cabinet/seats/*.toml` · `done`
*SPEC §7, ARCHITECTURE §132, §149, §554.*

`cabinet/seat.py` now loads `$DAEDALUS_HOME/cabinet/seats/<key>.toml` at startup,
seeding a file per built-in on first load; the literals are the fallback, so a
deleted file means "never mind" and a fresh install needs no setup step.
`Runtime.seats` carries the loaded set and `build_system_prompt` reads the charter
from it. `daedalus seats` lists them. Exactly one seat is active: two claiming the
owner is refused, none leaves the default.

**Acceptance:** editing a charter changes the system prompt; a broken file is an
actionable error; the built-in charters remain the fallback for a fresh install.
**Done** — 28 tests in `tests/test_cabinet.py`, including the prompt-level claim:
an edited `athena.toml` reaches the system prompt and the built-in does not.

### T12 — `cabinet/namespace.py`: seat memory namespaces · `done`
*ARCHITECTURE §12 ("identity + charter + memory namespace"). `Seat.namespace` exists and is used nowhere.*

Scope memory reads and writes by seat. Daedalus's own namespace must keep
behaving exactly as it does today, or every existing fact is orphaned.

**Acceptance:** a fact written under seat A is not recalled under seat B; a fact
written before this ticket is still recalled by Daedalus afterwards.
**Done** — the namespace is a directory: `memory/facets/` is `daedalus` (so nothing
moves on upgrade), other seats read `memory/namespaces/<key>/facets/`. The index
gained a `namespace` column, so an install whose index predates it is rebuilt from
the notes rather than mis-read; `rebuild_index` walks every namespace. `Runtime.memory`
became a property over `memory_root` so no call site can forget the scope. 42 tests in
`tests/test_namespace.py`, including an old-schema index built by hand and rebuilt, a
charter file naming a namespace that could escape the tree, and a scripted turn under
`athena` whose fact is invisible to the coordinator.

### T13 — Activate one seat · `done`
*SPEC §7, AGENTS.md. (The §11 gate this ticket waited on — "no seat is wired until
the proof-of-life holds" — was met on 17 Oct; what keeps a seat out of the delivered
default now is that wiring one is the next ticket, not a missing precondition.)*

Choose a seat for a turn, put its charter in the prompt, and keep **one** active
at a time. The default stays Daedalus.

**Acceptance:** activating a seat changes the identity and charter the model
sees; exactly one seat is active; the default is unchanged when nothing is set.
**Done** — `active = true` is now read: `pick(seats, None)` resolves to the active
charter, and `run_turn` resolves the seat **once** so the session, the prompt and the
memory namespace agree. `daedalus activate <seat>` writes every file whose flag
changes (one edit, not two), refuses an unknown name, and accepts a display name
(`Melinoë`). A wake speaks as the active seat. 21 tests in `tests/test_activation.py`,
including the exact failure this fixes: a turn with the flag set on `athena.toml`
whose system prompt still said "You are Daedalus".

### T14 — `cabinet/protocol.py` + `broker.py`: seat↔seat · `done`
*ARCHITECTURE §9.2, §7.5. Only needed once two seats must actually collaborate.*

The typed message shape, `cabinet/messages.db`, at-least-once delivery **with
dedup** (never exactly-once), correlation and causation links.

**Acceptance:** a message survives a restart; a redelivered message is deduped; a
reply carries the id of the request it answers.
**Done** — two modules, split at the seam that matters: `protocol.py` is the
message and its invariants (no I/O), `broker.py` is the durable queue over
`cabinet/messages.db`. Three rules carry the ticket. **The id is minted before the
first attempt** — a retry reuses it, so the second write stores nothing, keeps the
row's place in the queue, and reports `duplicate=True`; an id minted inside the
retry loop would be a new message every time and no downstream check could tell.
**Delivery is an offer, counted** — a message is offered until it is *resolved*, so
a seat handed it twice is told (`attempts=2`) rather than left to guess whether the
earlier attempt finished; the broker cannot know that, and asserting otherwise is
the exactly-once lie §9.2 rules out. **A reply inherits the exchange** —
`reply()` sets `causation_id` to the message it answers and `correlation_id` to that
message's `correlation`, so `thread(any id in it)` returns the whole exchange in
order and the route back cannot be passed in wrong. Also refused, because each is a
silent failure otherwise: an unknown `type`, a payload that is not a mapping, a
message from a seat to itself, and — the one that would sit in the queue forever
looking like a seat that never answers — a **display name** as a recipient
(`Melinoë`), refused with the key (`melinoe`) spelled out. 24 tests in
`tests/test_broker.py`, including the whole exchange across three brokers on one
path. Falsified: with the broker's state in the process instead of the file, the
five durability tests go red and the other nineteen still pass. Docs: SPEC §7,
ARCHITECTURE §7.5 and §9.2. **Nothing consumes it yet** — the queue is the
substrate; wiring a seat to read its inbox is the collaboration ticket, and that is
deliberate, because a broker nobody sends to cannot be wrong in a way this ticket
could have caught.

### T18 — Missed jobs queue and stagger on startup · `done`
*The owner's ask (16 Oct): "jobs are queued up when not run, and run when started
up but like not all at the same time, run only the needed tasks."*

A job whose tick fell while the daemon was down should be **queued, not
dropped** — and on startup the queue drains **staggered**, one at a time, never
a pile. "Only the needed tasks" is the queue's judgement: a probe whose moment
has clearly passed (a disk check from six hours ago) is skipped with a note
rather than fired late, while a morning briefing is still worth delivering late.

**Acceptance:** a job missed while the daemon was down runs after startup; two
missed jobs run one after the other, never simultaneously; a stale job is
skipped with a visible note.
**Done** — `scheduler/catchup.py` decides, and `Scheduler.catch_up` drains it
after the ordinary due pass (which is what keeps the two from double-claiming a
minute). Three rules, all explicit: only the **latest** missed occurrence is a
candidate (the earlier ones are counted, never replayed); `late_within` decides
whether even that one is worth it, defaulting per kind (`1h` for a capability,
`12h` for a wake) because a probe reports a *condition* and a wake talks about a
*day*; and a skip moves `last_run` too, so the verdict is taken once instead of
on every tick. The missed moment decides *what* runs, the present moment decides
whether it may speak — quiet hours still apply to a briefing caught up at 03:00.
22 tests across `tests/test_catchup.py` and `tests/test_cron.py`
(`cron.previous_run` / `count_occurrences`), plus a 14th selftest check that runs
the queue in its own scratch home. Falsified: with `catch_up` returning early,
the seven acceptance tests go red.

### T15 — P4: reach it from the phone · `open`
*SPEC §10 (P4), §3.5 (`push` / Taildrop).*

What T2 leaves: the dashboard over the tailnet, and the `push` output channel.

**Acceptance:** the dashboard opens from the phone; the tailnet is the only way
in; a `push` delivers a file over Taildrop.

### T19 — A declared cap is a cap · `done`
*`capabilities/runner.py`, `capabilities/manifest.py`. SPEC §13 (the `cpu` /
`mem_mb` line), ARCHITECTURE §9.3 and §11. Found 17 Oct, while reading the runner
for the seat work.*

Every manifest declares `[caps] cpu / mem_mb / time_s`. The values are persisted,
they are shown to the model that authored them, they are part of the artifact's
promise — and only `time_s` is enforced. Nothing stops an authored capability from
taking every core, or allocating until the kernel picks a victim, and the victim is
usually the daemon or whatever the owner is actually doing. **A boundary that is
written down and not enforced is worse than no boundary**, because the model
reasoning about its own resource use and the owner reading the manifest both act as
though it is there.

**Acceptance:** a capability that passes `mem_mb` is stopped and the failure names
the cap; `cpu` bounds the share of the machine it can take; `time_s` still stops a
hang; a capability inside its caps is unaffected; and every number comes from the
manifest, not from the runner.
**Done** — every declared cap is now a boundary, and the three answers are the
ticket. **`mem_mb` is enforced on resident memory**, by the runner sampling
`/proc/<pid>/status` while the artifact runs — not with a tight `RLIMIT_AS`, which
is exactly the trap SPEC §13 named: address space is not memory, and a healthy
CPython maps far more than it ever touches, so a tight limit kills working
capabilities. Resident is also what the owner means by "512 MB", and it is what the
failure can quote afterwards (`hit its mem_mb=64 cap: resident memory reached
116 MB`). A generous `RLIMIT_AS` stays, at four times the declared cap and never
below 2 GiB, as a backstop for one allocation large enough to land *between* two
samples — a documented secondary mechanism, never the cap itself.
**`cpu` is a share of the machine**, set as CPU affinity over whole cores, rounded
**down**: handing out the next core up would exceed what the manifest declared, and
a cap that is exceeded is not a cap. A sub-core share cannot be expressed this way,
so `cpu < 1` is *refused* with that reason rather than silently rounded up; the
cgroup quota that would do it is recorded in SPEC §13. Because all three are set in
the child (a stdlib launcher that `exec`s the artifact — not `preexec_fn`, which is
unsafe in a process that may have threads) they survive the exec and are inherited
by anything the artifact spawns, and the child now leads its own session so a
timeout or a breach takes the whole tree instead of orphaning helpers that keep the
caps they were started with.
Two smaller things fell out. Caps of zero are **refused where they are read**,
because a zero cap is a kill switch, not a boundary, and the authoring tool now
takes `cpu` and `mem_mb` too — a cap the model cannot declare is a hidden default.
And a failed run now reports its cause (the last stderr line, or the signal)
instead of blaming the absent JSON.
10 tests in `tests/test_caps.py` plus a 15th selftest check (`resource caps`, which
is also what would notice if `/proc` stopped being readable and `mem_mb` silently
went back to being decorative). Falsified both ways: neutering the sampler reddens
exactly the three breach tests, and dropping the affinity call reddens the `cpu`
test (`assert 16 == 1`). Docs: ARCHITECTURE §11, SPEC §13.

### T20 — A process that dies is visible, and outlives a restart · `done`
*`tools/process.py`. SPEC §13 ("long-running-process supervision"). Found 17 Oct.*

`process start` leaves something running and `process list` shows `rc=` — but
nothing ever says a process *ended*; the log is a list in memory; and the registry
is a dict on the manager, so a daemon restart forgets every process it started.
They keep running as orphans that `list` no longer shows and `stop` can no longer
reach, and the owner's only clue is a laptop behaving oddly.

**Acceptance:** a process that exits on its own is reported — its exit code and when
it ended are visible without asking; a process started before a daemon restart is
still listed, with liveness read from the real pid; `stop` on an entry that is
already gone says *that*, rather than "no process"; and a stop never signals a pid
that something else has since been given.
**Done** — the registry is now a file (`state/processes.json`), and it holds four
things per process: the command, the pid, **the pid's `/proc` start time**, and when
and with what code it ended. The start time is the load-bearing one. A pid is not an
identity — it is a number that gets recycled — so the pair is what lets a row
written by a previous daemon say "that was me" without ever saying it about whatever
holds the number now, which is the difference between stopping an orphan and killing
a stranger. An entry with no fingerprint (a hand-edited row) is therefore never
called alive and never signalled, because *not* killing a stranger outranks reaching
a process.
Three states used to look the same and now read differently: **running**, **exited**
(`exited at 09:12:01 (rc=3)`), and **ended with nobody watching** — a crash or a hard
kill, where the exit code was never ours to collect and saying otherwise would be an
invented number. The buffer of output is honest about the same seam: a process from
an earlier run reports `output not kept` rather than `0 lines`.
`stop` answers with what happened, not with consent: an entry that is already gone is
**not** stopped, and says when it went; a process started by an earlier run *is*
stoppable (the fingerprint has vouched for the pid), and the reply says the exit code
is not ours to collect. A second `start` under a live name is **refused** now —
it used to replace the registry row, leaving the first process running with nothing
able to list, read or stop it. And stopping actually stops: the process leads its own
session, so SIGTERM (then SIGKILL after ten seconds) goes to the whole tree, because a
killed shell does not take the command it was running with it.
**No restart policy**, deliberately: a process that exits may be exiting for a
reason, and a supervisor that silently restarts it is how one crash becomes a loop.
The ending is recorded and reported and the decision stays with the owner. 11 tests in
`tests/test_process.py`. Falsified three ways: an in-memory registry reddens six of
them; `_is_alive` trusting the pid alone reddens the recycled-pid test; and dropping
the ending-record reddens the reporting tests. Docs: ARCHITECTURE §5 and §8.5, SPEC
§13.
