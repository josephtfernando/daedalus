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

**Order:** T1 · T3 · T4 · T5 · T6 · T10 · T7 · T11 · T12 · T13 · T8 · T9 · T2 · T14 · T18 · T19 · T20 · **T21 T22 T23 T24 T39 (P3, seats)** · **T15 T25 T26 T27 (P4)** · **T28 T29 T30 (P5)** · **T31 T32 T33 (P6)** · **T34 T35 T36 T37 T38 (asked for 17 Oct) · T40 · **T41 (found 18 Oct, live-fixed, needs source)** · **T42 (found 18 Oct, the one-shot reminder contract — done)** · T43 · T44

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

---

## The road from here — P3 seats, then P4 → P5 → P6

*Written 17 Oct at the owner's ask. The seat order is the owner's: **Icarus and
Odysseus are worth it now**, then Melinoë, then Athena, then Calliope and Heracles —
each wired only as it earns its place. P4, P5 and P6 are tickets now instead of
intentions. Where `SPEC.md` has not decided something, the ticket says so and names
the decision; it does not invent a product to fill the gap.*

### T21 — Icarus and Odysseus, first · `open` — build done 19 Oct; closes on the first live delivery (Mon 08:00)
*SPEC §7 (seats), §10 (P3). The owner, 17 Oct: "icarus and odysseus are worth it now."*

Together, because they are already a pair in §7: Odysseus researches, Icarus builds.
"Wired" means the same four things for either seat — a charter on disk that says what
the owner wants *now* rather than what was written at first load, a namespace with
something in it, an activation that switches the identity cleanly, and **one thing the
seat owns that has actually run**. Icarus's headline job, the OpenCode delegate, is P5
(T28–T30); until then it earns its place on something else, or waits — this ticket is
the seat, not the delegate.

**Acceptance:** `daedalus seats` shows both with current charters; activating each
answers as that seat and its writes land only in its own namespace; a nameless turn
still answers as Daedalus; each has one scheduled or authored thing of its own that has
run and that the owner saw.

*Drafted 17 Oct — charters done, the rest open.* All eight charters were rewritten to say
what each seat owns rather than repeating §7's table, and two rules came out of it: the
frame no longer calls every seat the owner's chief of staff (it said so for all eight,
which was true of Daedalus alone and contradicted every other charter), and a charter no
longer names a tool (Icarus's listed the OpenCode delegate he cannot yet reach; the tools
listing already tells him). What remains here is the third and fourth clauses — a **thing
each of Icarus and Odysseus owns, that has run**, and the owner having seen it. The
charters being current is necessary and not sufficient: a seat whose work has never
executed is still a file.

**Done 19 Oct (overnight).** The mechanism clause landed: a scheduler job can carry a
`seat` (SPEC §7 was extended in the same commit), and a seated wake composes from the
seat's charter, writes into its namespace, and delivers under its name — with
refusals, not substitutions, when the seat names nobody (`schedule op=add` refuses
unknown seats at the door; a broken job at fire time is recorded, never delivered as
somebody else). Jobs stay file-first and the tick now re-reads `jobs.toml`, so a seat's
work can be added by hand and fire without a daemon restart. The live jobs, on the
owner's request of 17 Oct ("icarus and odysseus are worth it now"):

- **Odysseus** — `odysseus_sweep`, weekly Monday 09:30, a job-market and research sweep
  reported with sources: the thing his charter names first.
- **Icarus** — `icarus_workshop`, weekly Monday 08:00, an engineering health check (run
  the selftest, report what broke and what he did not test — his charter's honest edge).
  The OpenCode delegate remains his headline job, and it stays P5 (T28–T30); this is the
  "something else" the ticket held open for him.

The quiet-hours gate still applies at fire time, so the first live deliveries happen
tomorrow morning, inside the wake window; the full fire-→-compose-→-deliver-→-record
path is proven in `tests/test_seat_jobs.py` (7 tests, including a scripted `rt.wake`
turn proving a seat's writes land only in its own namespace). The owner reads the first
cohort's output at breakfast — "the owner saw it" is then witnessed in the journal
(`journalctl --user -u daedalus.service`), same standard as §11's acceptance.

### T22 — Melinoë, next · `done`
*SPEC §7 (verification, privacy, approvals), §3.5 (the Melinoë gate).*

Melinoë already *acts* — the gate runs every authored capability before it may be
scheduled — but she is not yet a seat: her verdicts are the coordinator's sentences,
and what she has refused is not anywhere she can answer from. The interesting half is
not the switch, it is that a refusal should be remembered *by her*, with the reason,
so that the next capability in the same shape meets the same answer.

**Acceptance:** her namespace holds what she verified and what she refused, with
reasons; activating her answers from it; re-authoring a capability she refused
re-triggers the gate rather than sliding past it; a nameless turn is still Daedalus.

**Done 19 Oct (overnight).** Both gate call sites — `capability op=author` and
`op=verify` — now write every verdict to `memory_for(seats["melinoe"])`, facet
`verdicts`, origin `local_derived`, with the capability, the verdict, the check
summary (`run_once — exit 3`) and the standing rule on one line ("refused: ... re-author
or re-verify ...; a refusal is never slid past"). Activating her, her prompt carries
the bench — the second test drives `build_system_prompt(ctx, event, seat="melinoe")`
and asserts the refusal is in it, and that the coordinator's *own* prompt is not. The
re-authoring clause is a test: refuse a broken artifact, re-author the same name with
a passing one, the gate runs again end to end and only the fresh pass verifies. A
bench that cannot take a note logs a warning (`record_verdict` never raises into the
gate). SPEC §3.5 carries the paragraph. 4 tests in `tests/test_melinoe.py`; suite
916 green. *The re-author path needed no code — the gate already ran on every
authoring; the memory makes the verdict *hers* instead of the turn's exhaust.*

### T23 — Athena · `open`
*SPEC §7 (knowledge and coding mastery — DSA, aptitude, competitive programming,
interview prep; pairs with Odysseus).*

The seat with the most obvious recurring job, and the one that leans on the scheduler
rather than on a conversation: practice and strategy need a cadence, not a reply.

**Acceptance:** her charter is current and her namespace holds her own progress; one
scheduled thing she owns runs on its cadence and reports to the owner; what she
remembers is invisible to the other seats and to the coordinator's namespace.

### T24 — Calliope and Heracles, last · `open`
*SPEC §7 (creative voice and distribution; the body — training, nutrition, sleep).
AGENTS.md (personal data is out of bounds).*

Last on the owner's list, and the two with a boundary in front of them rather than a
mechanism. Calliope's work ends in *distribution* — publication is an egress action,
so it needs a grant and an approval posture before it can do anything real. Heracles
is the first seat whose subject matter is the protected category: training, nutrition
and sleep are personal data, and AGENTS.md keeps them out of bounds until the owner
opens them, deliberately and in writing.

**Acceptance:** for Calliope, a piece of writing reaches the owner and *publishing* it
is a proposal with a grant, never an autonomous move; for Heracles, **the owner has
first decided and recorded what it may read, what stays on the laptop, and whether a
remote model ever sees it** — and the seat is wired only to what that decision allows.

### T39 — the coordinator routes a request to the seat it belongs to · `done`
*SPEC §7. The owner, 17 Oct: "I want daedalus to be able to figure out who is best suited
to do things, and then route my message to them."*

Asked for straight after the cabinet was drafted, and it is the clause that makes the
seats real: without it a seat is reachable only by the owner naming it, which is a
switchboard rather than a chief of staff. **`cabinet/triage.py`.** An address the owner
wrote wins with no model consulted (`athena: …`, `@athena …`, `to athena: …` — and a
leading word that is not a seat is an ordinary sentence, per §7). Otherwise one
cheap-route call asks which seat the message belongs to, with **the charters themselves
as the menu**, because a second list of responsibilities living in code would drift the
day a charter was edited and would be the copy the model believed. The answer is
validated against the cabinet so routing cannot invent an identity; anything unreadable
falls back — the seat already handling this conversation, else the active one — because
a classifier's bad day must not cost the owner their request. A routed turn is run as
that seat from its first token (its charter, its memory namespace, its voice), and the
reply names whoever took it whenever that is not the identity the owner was already
talking to. *The engine is untouched: routing is decided at the ingress and passed in as
`seat=`, so a caller that already knows the identity (a wake, the self-test) is
unaffected.*

**Acceptance:** a message about practice is answered by Athena and one about hiring by
Odysseus, without the owner naming either; a seat addressed by name is never re-judged;
an answer naming a seat that does not exist routes nowhere; a message that is a follow-up
stays with the seat already handling it; and a routing failure leaves the request on the
active seat.

*Done 17 Oct.* `triage()` (with `addressed` / `read` / `menu` / `prompt`) and its wiring
at both ingresses — the Discord handler and the CLI repl, plus `daedalus ask`, which was
also **printing nothing at all** when it succeeded and now delivers like every other
path. 22 tests in `tests/test_triage.py`, and one live run: "give me three DSA topics to
drill this week" routed to Athena (`by=model`, *"she owns DSA practice and sets what the
owner drills"*) and she answered with a plan built from the real calendar — contest Sunday,
graphs Monday and Tuesday, DP Thursday and Friday, Wednesday left free. Falsified twice:
letting `read()` accept an unresolvable name reddens the invented-seat and
mentions-a-seat tests; letting a provider failure propagate reddens the two fallback
tests. Docs: SPEC §7, ARCHITECTURE §7.1 and the new §7.1.1. *Cost, stated plainly:
routing adds one cheap-route call to every owner message (messages under four characters
skip it), which is the price of not having to name a seat.*

#### P4 — remote / Tailscale Serve

### T15 — P4: reach it from the phone · `open`
*SPEC §10 (P4), §3.5 (`push` / Taildrop).*

What T2 leaves: the dashboard over the tailnet, and the `push` output channel.

**Acceptance:** the dashboard opens from the phone; the tailnet is the only way
in; a `push` delivers a file over Taildrop.

*Note (17 Oct, measured): the tailnet route was live but dangling — `tailscale serve
status` proxied `127.0.0.1:43945`, where nothing was listening, while the daemon serving
today's dashboard was on 40169. Publishing mapped the port of the run that published it,
so a restart moved the port and left the route pointing at nothing. The phone path was
broken and nothing reported it. **The route half is fixed** (T27, 17 Oct): the daemon's
dashboard binds a fixed port and publishes it on start, so the route now points at the
live port or is down. What remains of T15 is the `push`/Taildrop leg and confirming the
dashboard from the phone itself — which needs the owner's phone.

*Owner action (17 Oct): the tailnet has a stale second `delphi` node — offline since
27 Sep, a duplicate registration of this laptop — and a node cannot remove another node
from the CLI (`tailscale` has no peer-removal, and no `TAILSCALE_API_KEY` is provisioned
on this machine, nor should the agent go looking for one in `.env`). It is a 20-second
console action: <https://login.tailscale.com/admin/machines> → the `delphi` row last seen
27 Sep → Remove. If the owner would rather Daedalus could do it, that is a `tailscale`
resource (access + credentials, T9's pending→adopted shape) rather than a code change —
and removing a device is destructive, so it stays a proposal either way.*

### T25 — the agent's own `serve` · `open`
*SPEC §3.5 (output channels: `serve`, `push`). §8.5 step 11.*

T2 publishes the **dashboard**, from a CLI flag the owner runs. Nothing lets the agent
hand over anything else it made — a page, a report, a project it built under T29 — even
though §3.5 files `serve` and `push` as *output channels*, which is to say things the
agent uses rather than things the owner runs. The gate for this already exists and is
right: `genui/publish.py` prints the command before running it, refuses when Funnel is
on, and never builds a Funnel command. Reuse that posture rather than re-deciding it.

**Acceptance:** from a turn, the agent publishes one named artifact to the tailnet and
the owner opens it on the phone; the URL it reports actually resolves; the artifact has
a lifetime and can be taken down; publishing is impossible without the tailnet in front
of it, and a node with Funnel on is refused, not warned about.

### T26 — the calendar from the phone · `open`
*SPEC §5.5 (a full dashboard view), §13 (calendar dashboard scope — the open decision).*

**Corrected 17 Oct, by checking instead of remembering.** This ticket was written from
the assumption that only the day view existed. It is wrong: `genui/dashboard.py` carries
`VIEWS = ("day", "week", "month")` and `genui/server.py` routes `/dashboard`, `/week`,
`/week/<date>` and `/month`, with tests covering each (`tests/test_dashboard.py` — the
whole point of the month test is "days that are not the first of anything"). So the
month view is *built*, and this ticket is much smaller than it read. What is actually
left is (a) that the phone cannot reach any of it today, because the route is dangling
(T27), and (b) the scope §13 genuinely left open — which **edit** operations are worth
offering on a small screen, given T8 already routed dashboard writes through the same
authorization the `calendar` tool needs.

**Acceptance:** each view opens from the phone once the route is live; an edit made from
the phone is refused without the authorization the `calendar` tool requires (T8's
comparison test extended to the remote path); nothing on the small screen can act on a
*series* while naming one instance (the failure T8's own work exposed).

### T27 — still there in the morning · `open`
*SPEC §8 (the laptop stays awake with the lid closed; `enable-linger`). §3.5.*

Three measured facts on 17 Oct, one theme — the machine and its route outlive the run
that set them up, and nothing notices when they stop working:

- **The lid.** Effective `HandleLidSwitch` is `suspend` (`busctl get-property
  org.freedesktop.login1 …`), while SPEC §8 says the laptop must stay awake with the lid
  closed because remote access and overnight work depend on it. The wizard prints the
  `/etc/systemd/logind.conf` lines for the owner to apply with sudo, and nothing checks
  whether they were applied.
- **The dangling route.** *Fixed 17 Oct.* Publishing mapped the port of the publishing
  run; a restart moved the port and left the tailnet route pointing at a dead one.
  Measured: route → 43945, daemon → 40169, nothing listening. The phone showed a
  connection error and no log said why. The daemon's dashboard now binds a **fixed
  port** (`[ui] port`, default `8787`), **publishes it on start** — which also heals a
  route a killed run left behind — and **takes it down before the server stops**;
  a busy port or a refused publish is reported rather than published over. 9 tests in
  `tests/test_daemon_ui.py`; falsified by putting the port back to `0` and dropping the
  publish call (three red).
- **The two that were already right.** Linger is on and Funnel is off — worth asserting
  so they stay that way.

**Acceptance:** closing the lid does not end remote access, asserted against the
*effective* logind configuration rather than a comment; ~~after a restart the published
route points at the live port or is taken down, and a mismatch is reported instead of
left~~ (✓ 17 Oct, above); the selftest says plainly when the lid will suspend it (it
cannot fix that itself — sudo — so it must not stay quiet about it). **Still open:**
the lid half — the effective logind check in the selftest, plus the linger/Funnel
assertions that make "still there" a claim the machine makes about itself rather than a
claim the ticket makes about it.

#### P5 — OpenCode delegation

### T28 — `opencode` becomes a resource · `done`
*SPEC §3.5 Layer 3 (a resource is access + credentials, provisioned once, listed at
startup), §5 (local CLIs include `opencode`), §8.5 stage 8 (optional resources).*

`opencode` is installed (`/usr/bin/opencode`) and named in the spec as a local CLI, and
`Resource.class_` already carries `local_cli` — so this is provisioning, which by the
spec's own rule is never a code change. The point of listing it at startup is that the
model knows what it may reach *before* it plans: discovering a missing CLI halfway
through a delegation is how a long task dies at the end.

**Acceptance:** `opencode` appears in `resources/registry.toml` as a `local_cli`
resource with the scopes it may act in; it is listed to the model at startup; the
wizard's optional stage provisions it; when it is absent the model is told it is absent,
not left to find out by failing.

*Done 17 Oct.* `resources/registry.py` gained `provision_local_cli()` (an **active**
`[[resource]]` entry — a local CLI has no credential to comment out, because the entry
*is* the grant) and a `PATH` check in `inventory()` for every `local_cli`, so a
provisioned-but-missing binary reads `— NOT INSTALLED (not on PATH)` in the prompt the
model gets at startup. Scopes are `workspace` (a named workspace it may touch — T29) and
`net` (its model). The wizard's optional resources stage provisions it either way and
reports whether it is on `PATH`. Reading a registry no longer trusts `added_at` to be a
number: a TOML datetime, an ISO string and an unparseable value are all survived, because
a hand-edit must not be what stops a startup. 8 tests in `tests/test_resources.py`.
Falsified: always reporting a local CLI as installed reddens the missing/absent/prompt
tests (three red). Docs: ARCHITECTURE §8.3, CHANGELOG. *Supersedes: `opencode` named in
the spec and absent from the registry.*

### T29 — a delegation goes out into a workspace and comes back reviewable · `done`
*SPEC §7 (Icarus owns the OpenCode delegate), §3.5 (`git` is its own egress action).
ARCHITECTURE §698 (P5 → `resources`, `capabilities`).*

The shape matters more than the CLI. A delegation is a task, a **named workspace** it
may touch — and the personal tree is not one of them — and a result the owner can
review: what changed, what was run, what it could not do. It never commits, never
pushes, never merges. Those stay with the owner, and §3.5 already files `git` as a
separate egress action needing its own grant; letting a delegate perform them would
quietly merge two trust decisions into one.

**Acceptance:** a delegated task returns a diff plus a run record; a delegation naming a
path outside its declared workspace is refused *before* the CLI is spawned, not after;
nothing the delegate does reaches the network without the egress grant the tools already
require; and no path it takes results in a commit, a push or a merge.

**Done 19 Oct (overnight).** `src/daedalus/delegate/runner.py` + `delegate` tool. The
driver-of-record for the return: default `cmd` is `opencode run <task>` in the
workspace; tests drive stub commands instead because what is under test is the shape.
The diff is against a baseline commit the daemon makes once at workspace creation
(`prepare`), so first delegations are also reviewable; `add -N` makes new files
visible to `diff HEAD`. The refusal-before-spawn clause is two checks in `check()`:
banned-history words in the task text (refused with the phrasing fix: "phrase the
task as work that leaves a diff") and `paths_outside()` — absolute/`~`/`..` tokens
resolved against the workspace, with the personal tree named *as* the personal tree
(`forbidden=(settings.home,)` wired from the tool). The no-commit clause is enforced
twice: task-text refusal, and the `git` PATH shim — hooks were never the enforcement
because `--no-verify` exists. This acceptance's network line rides on the machinery
that already exists: the delegate child inherits `scrubbed()` (secrets out of its
environment), and egress-scoped work remains the owner's grant — recorded in the
ticket and the task text rather than re-implemented here. 7 tests
(`tests/test_delegate.py`); suite 922 green.

### T30 — a long delegation is visible, capped and cancellable · `done`
*SPEC §9 (long tasks run in threads). T19 (capability caps), T20 (the process registry).*

An OpenCode run is minutes, not seconds — longer than a turn's patience and exactly the
case T19 and T20 were built for. This is where they earn their keep: the delegation runs
as something with a pid, its own `mem_mb` / `cpu` / `time_s`, live status, and a way to
stop it. Its progress belongs in a thread (§9) rather than in the turn's reply.

**Acceptance:** a delegation is listed with live status while it runs; exceeding its cap
stops it and the stop is visible with the cap named; cancelling from the surface kills
the whole tree, not just the process the daemon happens to hold a handle to; when it
finishes, its exit and its output are still readable afterwards.

**Done 19 Oct (overnight).** The delegation is adopted into the same registry T20
keeps (`ProcessManager.adopt`) under `delegation <workspace>`: `ProcessManager._load`
rows from previous runs still resolve (the fingerprint vouches for the pid, so
`op=status` after a daemon restart says "ended with nobody watching" where true
rather than "running" where hoped). Caps come through `limits.watch_caps` — the
watcher the extraction finally made shareable: resident-memory breach stops the tree
and the error *is* the sentence naming the cap ("hit its mem_mb=64 cap: resident
memory reached 210 MB", tested); `time_s` is the wall ceiling (timeout named in the
same place); `cpu` is set in the child by the launcher, caps intact. Cancelling is
the registry's tree-wide stop (`asyncio.create_subprocess_exec` with
`start_new_session=True`, so killpg takes helpers too) — tested: a live delegation
shows `alive()`, `stop` reports `stopped (pid, rc=…)`, and the collect task then
closes the row instead of leaving `rc=None`. `op=result` reads the run record from
the workspace afterwards — exit and output are files beside the work, not state the
turn held. The end is announced on the surface with the exit code and the diff size,
failures same as successes (T41's lesson: a silent failure while the owner reads
something else is a lost failure). *Progress-in-a-thread*: `op=run` returns in the
turn, the ending arrives as its own surface message — the thread-parking of the
progress ledger itself is the turn's existing machinery, and is recorded here as the
one clause this feature does not own.

#### P6 — the group chat

### T31 — a seat reads its inbox · `open`
*SPEC §7 ("seats do not call each other — they leave messages"). T14 (`broker.py`, done
and unwired). ARCHITECTURE §9.2.*

P6's first honest step, and the one T14 deliberately stopped short of: the queue is
durable, tested, and nothing calls `broker_for(home)`. A seat with a pending message must
be **offered** it when it runs, be able to answer, and have that answer resolve the
message — with the offer counted, so a second delivery is visible rather than silent.
The rule that makes this safe is already in the substrate: a message's id is chosen
before its first attempt, so a retry is the same message and the second answer to a
resolved one is recognisably the duplicate.

**Acceptance:** a message left for a seat is delivered on that seat's next turn and
resolved by its answer; an unanswered message is offered again later; a repeated offer
is *reported* as another attempt, not hidden; reading a message never deletes it, and a
reply carries the exchange it answers.

### T32 — a room is a bounded exchange · `open`
*SPEC §7 (the typed vocabulary, including `verify`). **The decision this needs is the
owner's, and SPEC does not make it yet.***

A group chat among seats is a different product from seats the owner addresses, and the
difference is almost entirely *bounds*. This ticket exists to force the decision into
`SPEC.md` before any code: who may convene a room; how many hops an exchange may take
before it must stop; what a seat may spend while it participates; what happens when
nobody answers (a message offered forever is a queue that never drains, which is the one
failure at-least-once delivery guarantees); and whether a seat's work has to be
**verified** — the `verify` message type exists for a reason — before its answer counts.

**Acceptance:** the bounds are written into SPEC first, then enforced in code and
tested: an exchange that would exceed its hops or its budget stops and says why, and an
exchange nobody will answer ends as a reported dead end rather than as a queue that
grows.

### T33 — the owner sees the room · `open`
*SPEC §9 (the surface). §3.6 (visual → dashboard, action → Discord).*

The room is only the owner's if they can watch it and interrupt it. Where an exchange
appears (a Discord thread, the dashboard, a served page — T25 makes the third possible);
whether seat-to-seat traffic is shown **as itself** or summarized into a single voice,
which are genuinely different products and not a formatting choice; how the owner joins
or steers mid-exchange; and what is kept afterwards, for how long, and readable how.

**Acceptance:** an exchange is readable from the surface while it happens; the owner can
join it and stop it; what is kept can be read back later; and if seat traffic is
summarized rather than shown, the summary names which seats spoke and the full exchange
is still recoverable — otherwise the decision to summarize has silently become a
decision to discard.

---

## Asked for on 17 Oct — files on the road, a surface worth looking at, three trackers

*The owner's own asks, written down the day they were made. Two of them are P4/P6 work
that was going to arrive anyway (files over the tailnet, the dashboard's look); three are
new capability work for named seats. The verification that prompted this section is in
T26's correction and in T27's measured notes: P4 is mostly built, P5 is not started, P6
is substrate only.*

### T34 — files, from wherever you are · `open`
*SPEC §3.5 (output channels `serve` / `push`; external action `file`). The owner, 17 Oct:
"a path that could serve files for me through Tailscale, when I'm away from my laptop,
like being able to query for files within my laptop through Discord and either display
the file or let me download it."*

Two halves, and the second is the one with teeth. **Asking:** a Discord message names or
describes a file, the agent finds it inside a *declared* root and answers with either a
rendered view (a page can show text, markdown, an image) or a download offer.
**Handing it over:** the download is served over the tailnet through the same path and
posture T2 established for the dashboard — loopback-bound, no Funnel ever constructed,
the command printed before it runs — with a lifetime and a one-time token, and taken
down when it expires.

The bounds are the design, not the polish: which roots are reachable (the workspace and
whatever the owner declared; never `.env`, `docs/private`, the vault's `private/`, the
secrets store), a size cap, and — the rule this ticket exists to hold — **serving a file
is an egress action**. Everything else in this system keeps values local and moves only
structure; a download is a value leaving the laptop, so it is the owner's move, taken
deliberately, and the *guard* decides what is reachable, never the model's confidence
that a path looked fine.

**Decided 17 Oct.** The first root is **this repo** (`/home/apollo/projects/daedalus`)
and nothing else. The root list is the owner's own data rather than code — adding a
folder later is a change to a file the owner owns, not a change to the program — because
the surface for it arrives with a dashboard the owner will ask for later, and a surface
cannot edit a constant. Widening what may leave the laptop is an **approval**, in the
shape T9 already established (a proposal that is pending until the owner adopts it),
since that is exactly what adding a root is: reach growing.

**Acceptance:** from Discord, a request for a file under a declared root returns either a
rendered view or a working download link; a request outside the declared roots is refused
by name, with the root it was refused against; the served URL is tailnet-only, carries a
one-time token, and stops working when it expires; naming a protected file directly does
not make it servable; the agent cannot publish a root it was not given; and the root list
is owner-owned data — a later dashboard can add a folder without a code change, and
adding one goes through an approval rather than taking effect silently.

### T35 — the surface stops being one page in one mood · `open`
*SPEC §3.6 (visual → dashboard; "the mood token set (the owner's)"). The owner, 17 Oct:
"the dashboard surface is too static and not very aesthetic … dynamic for what I asked
for in that moment, or just an overall aesthetic dashboard with a custom theming engine."*

**Ground truth first, measured 17 Oct** — the dynamic half of this is more built than it
feels from the phone, and the aesthetic half is where the emptiness is:

- The calendar views are done (see T26's correction): day, week and month render today.
- The **theming engine is real**: tokens are validated by kind (`_COLOUR`, `_LENGTH`,
  `_FONT` with a forbidden set), resolved from server mood → owner overrides →
  adopted tunes (`genui/theming.py`, `tuners.py`, `tokens/approved.toml`), with a draft
  preview path.
- What is thin is the *content* of that engine: **two moods** (`calm`, `celebration`)
  and **five components** (`list`, `timeline`, `table`, `prose`, `calendar`) — and
  `genui/tokens/` and `genui/components/` on the real home are **empty**, meaning the
  engine has never been used in anger and every page has rendered on built-in defaults.
- "Dynamic per moment" is already the architecture — the model composes a spec, code
  binds local values — but the standing dashboard composes the same page every day,
  which is what "static" is actually describing.

**Decided 17 Oct (owner).** shadcn/ui is the direction: *"shadcn is good, it'll be good
to use a framework like it for the future when I want more complex layouts, also it helps
defining a fixed set of primitives to use for everything. I want consistency."* That
resolves the fork below in two moves, not one: the **design language now** — a real
spacing/radius/shadow/type scale, dark and light first-class, more moods than two, and a
**fixed set of primitives** every page composes from, so consistency comes from the
vocabulary rather than from discipline — and the **toolchain later**, sanctioned for the
day the owner wants complex layouts, recorded then as superseding §3.6's no-script
property rather than eroding it quietly. The ticket is unblocked; what follows is the
reasoning that decision was made against.

**The honest fork.** `shadcn/ui` is React + Tailwind + a Node
build. This page is one self-contained document that fetches nothing and runs no script
(`CSP default-src 'none'`), and that property is load-bearing — it is how §3.6's "the
model composes structure, code binds values" stays structural rather than aspirational,
and it is why the dashboard works on a phone over a tailnet with no build step. Adopting
the toolchain buys components at the cost of that property. Adopting shadcn's **design
language** — a real spacing/radius/shadow/type scale, dark and light as first-class,
more moods than two — costs nothing and is where the gap actually is. Recommendation:
the design language and a CSS-only interaction set (`<details>`, `:target`, form posts,
which the controls already use), not the toolchain.

**Acceptance:** the owner asks for a month, a week, or what is left today and gets it
from the surface without editing code; the mood set has real range and the owner's
adopted overrides visibly render; the component vocabulary grows by *proposal* (new
entries through the same validation door, not drift); a page still fetches nothing and
runs no script unless the owner has explicitly opened that door — and if the toolchain
itself is chosen, that is recorded as a decision that supersedes §3.6's no-script
property rather than quietly eroding it.

*Carried over from T34 (17 Oct): the surface that edits the **servable root list** lives
here. T34 made the roots owner-owned data rather than code, so that a folder can be added
without a code change; this is the ticket that gives the owner the screen to do it, with
T9's adopt-before-it-counts shape behind it.*

### T36 — the habit tracker · `open`
*SPEC §3.5 (capabilities are local scripts first). SPEC §7 (the seats it belongs to).*

The smallest of the three, and the one that needs a cadence rather than a conversation —
which makes it the natural first customer of the scheduler. A habit is a small daily
fact: did it happen, when, how many days in a row. State lives in the capability's own
state directory. **Open question for the owner, recorded rather than guessed: whose seat
is this?** The body is Heracles', but habits are also the coordinator's territory —
Daedalus is the one who guards sleep and writes the honest weekly audit.

**Acceptance:** the owner records a habit by message and it is remembered; marks and
misses are recorded with their date; asking for "how am I doing" answers from the
capability's own state rather than from the model's memory; a missed day is visible as a
miss, not silently absent; the streak it reports is the streak the stored days imply.

### T37 — the LC tracker · `open`
*SPEC §7 (Athena — DSA, aptitude, competitive programming, interview prep).*

LeetCode practice, for the seat whose whole charter is getting better at exactly this.
The useful shape is not a counter — it is *coverage and staleness*: which topics have
been touched, which have gone cold, what is due for a repeat. Athena reads it on her
cadence and speaks from it.

**Acceptance:** the owner (or the agent, from a turn) records a solved problem with its
topic and result; the tracker reports per-topic progress and what has gone stale;
its record is Athena's namespace, so what she "remembers" about practice is the stored
record and not a recollection; recording the same problem twice updates it rather than
duplicating it.

### T38 — the workout tracker · `open`
*SPEC §7 (Heracles — the body: training, nutrition, sleep). AGENTS.md (health data is
out of bounds; structure travels, values stay local). T24 (Heracles is the seat with a
boundary in front of it).*

Training data is personal data, and this is the first capability whose whole content is
in that category — so its posture is part of its acceptance rather than a footnote
added later. Storing it is fine and ordinary: it is the owner's own record, on the
owner's own laptop, at `0700`. The two things that must be designed rather than assumed
are (a) what may be *read* by whom, and (b) that these values never reach a remote
model. That is why this tracker's reporting is deterministic code plus, if anything,
the local model — an exercise log is not worth sending to a provider, and "it was only
a number" is exactly the reasoning this rule exists to pre-empt.

**Acceptance:** the owner records a session (what, how much, when) and it is remembered;
the progress it reports is computed from stored sessions; **the owner has recorded what
this may read and what may never leave the laptop, and the capability is wired only to
that**; no remote model call carries a workout value, checked as a property of the
capability's egress rather than trusted to its prompt.

### T40 — the wizard provisions the tailnet path · `done`
*SPEC §8.5 stage 8 ("Resources (optional, skippable) — gcal, mail, tailscale"). The
owner, 17 Oct: "setup a wizard for tailscale thing".*

The spec's stage 8 has named tailscale since the P0 draft, and the wizard honoured it with
an advisory `command -v` and a Hardening line that said "nothing to assert". Meanwhile the
phone path became real — a fixed port, published on start, taken down on stop (`fccf190`)
— and the stale `delphi` node needed a console action no CLI could take (T15's note). So
stage 8 now actually provisions it:

- **Install and login are asserted, not narrated** — present/logged-in are checked;
  otherwise the exact commands are printed and the skip is recorded as a skip.
- **Funnel is asserted off**, read the same way `genui/publish.py` reads it (the JSON's
  `Funnel` map, non-empty = on; unreadable = *unknown*, warned about, never a pass). A
  shared `_funnel_state` helper serves both the resource stage and Hardening, which now
  asserts instead of its "nothing to assert" note.
- **A Tailscale API key is capturable as a resource** — pasted through hidden entry,
  sealed into the credential store through a 0600 temp file that is then deleted, the
  variable unset the line after its last use, and noted in `resources/registry.toml` as
  `remote_api` / `apikey` scoped `devices.read,devices.remove`: a commented placeholder,
  the same proposal shape gcal gets. With it, tailnet hygiene — removing a stale
  duplicate node — becomes something Daedalus can *propose* under T9's pending→adopted
  shape instead of a console action. Removing a device is destructive, so it is never
  autonomous.

**Acceptance:** the posture is tested, not narrated — no line of the wizard can turn
Funnel on; the key reaches the store and never `.env`, config.toml or the wizard's output;
the registry note is a commented proposal, not a claim of access; and the wizard's Funnel
reading agrees with the publisher's on the same inputs.


## Found while working — 18 Oct

*Two defects found by running the thing rather than reading it. Both were silent: the
system kept working, so nothing surfaced. The first is fixed on the running instance; the
second is the reason it happened and needs a source change.*

### T42 — a wake that is not a cron cannot be said · `tools/schedule.py`, `tools/remind.py`, `scheduler/` · `done`

**Symptom.** 18 Oct 22:51, routed to Calliope: the owner asked for a reminder to send
a piece of his own writing. The turn tried `schedule op=add` four times in four steps
(journald: 22:52:24 · 22:52:27 · 22:52:31 · 22:52:35, identical 253-char answers) and
every call was the same shape:

```json
{"kind": "wake", "late_within": "2h", "name": "cmrlsim_voice_sample", "op": "add",
 "target": "Remind the owner to send a piece of his own writing…"}
```

After the third identical failure the engine appended its correction — the full error,
"Do not send that call again unchanged… if the error says what it wanted, do that" —
and the fourth call went out **unchanged four seconds later**. The turn ended with
`error=repeated failure` and a
"tell me what to try instead" handed to the owner.

**Cause — three stacked layers, in order of blame.** The engine behaved as built
(three failures → one correction → one unchanged re-send → a clean stop); this is a
contract failure, not an engine one.

1. **The schema says `cron` is optional.** `schedule`'s JSON schema marks `name`,
   `cron`, `kind` and `target` all optional (`tools/schedule.py` line 27-40) — the
   requirement lives only in a runtime error the model has to fail into. A single flat
   schema carrying three ops cannot express "required for `op=add`", and nothing in
   the tool description or the parameter descriptions says so.
2. **The intent is inexpressible.** The owner wanted a **one-shot** reminder. `cron`
   is the only time dimension `schedule` has, and no cron expression says "2 hours
   from now, once" — even an absolute-date one (`30 15 18 10 *`) recurs *yearly*. The
   model reached for `late_within="2h"` because its description read like a delay;
   it is the catch-up window for runs missed while the daemon was down. And the
   correction's escape ladder — follow the error's fix → a different tool → a
   different argument — was empty at every rung: the fix demanded a cron that cannot
   say it, no tool existed for one-shots, and `late_within` is not a time. The one
   legal rung ("ask a question") went untaken — the model's share of this.
3. **The trap is original-sin, not a regression.** `git log -S "add needs"` lands on
   `0b07899` — the P0 commit itself. Every one-shot intention since the loop shipped
   has met the same wall; the acceptance run's sixteen-step identical retry
   (`loop/engine.py` line 210) is the same trap in its oldest recorded form.

A fourth, cosmetic layer: the pasted error read `"0 7 * * "` — **Discord's markdown
italicises pairs of `*` and ate the trailing one from the engine's canned message**,
which interpolates raw error text. (The live work-trace is safe — it code-spans each
line.)

**Fix.**

1. **A `remind` verb** — one-shot wakes as their own contract, not a special shape
   inside `schedule op=add`: `remind op=add` takes `in` ("2h") *or* `at`
   ("2026-10-18T17:30"), exactly one, plus `note` (the wake prompt). Underneath, a
   `Job` whose `run_at` is mutually exclusive with `cron`; it fires once through the
   existing wake path (`fire_reminders` → the wake handler) and then **retires
   itself**, so a reminder cannot become a yearly cron by accident. `late_within`
   stays available as the honestly-described grace window. Plumbing, not a new
   runtime concept: the post-turn engine, quiet hours and the daily cap are inherited
   unchanged.
2. **Schema honesty for `op=add`** — `cron`, `name` and `target` say "required for
   `op=add`; omit for list/remove" in their descriptions, and the tool description
   says `add` is recurring only, with one-shot reminders belonging to `remind`.
3. **The error teaches the way out** — `add needs cron` ends with "for a one-shot
   reminder, use `remind`", which makes the correction's "if the error says what it
   wanted, do that" branch *followable* instead of a dead end. This is the
   difference between the 22:52 turn succeeding and failing.

                  schedule op=add` without `cron` is refused with the
`remind` path named in the refusal; the schema the model sees states the per-op
requirements; and the canned "stuck" message renders the tool's error un-mangled
(the trailing `*` of a cron example survives Discord).

**Done 19 Oct.** The verb is **`remind op=add`** — `within` ("2h") or `at`
("2026-10-19T14:00"), exactly one, plus `name` and `note` — on a `Job` carrying
`run_at`, mutually exclusive with `cron`. The reachable pieces were built as designed;
two semantics were **decided better than the ticket first said**:

- **Missed-late reminders deliver; they never skip and never silently forgive.** The
  first acceptance wording ("past its window, the missed wake is skipped") would have
  meant an owner-named alarm dying quietly in a log the owner does not read — the
  T41 sin. So a delivery far past its moment is caught up by the next tick, **prefixed
  with how late**, and retired. Delivery itself is never gated: the reminder bypasses
  the proactive gate exactly as calendar alarms do, because the owner named the time.
- **A failed compose keeps the reminder and retries**, so a transient model outage
  cannot eat it. A crash between fire and retire cannot redeliver — the one-shot's
  idempotency key is anchored to the named moment, not to the tick's minute.

`catchup.plan` skips one-shots (the cron scan has nothing to say about them). Schema
honesty: the `schedule` description states `add` is recurring-only, the parameter
descriptions name their per-op requirements, `late_within` says outright it is *not*
the delay, and the `add needs cron` error signs the way out. The system prompt names
the verb next to the speak-first paragraph. SPEC §3.5 lists `remind` among the
primitives and §5.5 records the gate-exemption rule beside the calendar-alarm one.
11 tests in `tests/test_remind.py`; the whole suite red-set is unchanged from the
in-flight T29/T30 baseline (22, identical), 883 passing. T44's part — the error
travelling fenced — landed the day before.

### T43 — memory capture is named as a habit, not left to personality · `loop/context.py` · `done`

**What was missing.** The owner tells the agent a person, a contact, a date, a
commitment — and the turn keeps none of it unless the model decides to. The store
was never the missing half (§5's supersession, bitemporality and `local_only` gate
are all there); what was missing was an instruction that makes capture part of the
turn. On 18 Oct the owner told Calliope a fact about his own writing and got an
answer that recorded nothing.

**Done.** The system prompt carries a `### Remembering` section above the
recent-memory block it reads from: a durable fact is written in the turn it is
said (`memory op=write`), into a facet naming the subject (`people`, `contacts`
suggested); what it replaces is superseded by `[id]` rather than contradicted
beside; and banter, calendar-resident facts and next-week ephemera stay out. No
hook, no pipeline — the prompt is where the other behavioural contracts live, and
the store's guarantees keep a prompted capture honest. `SPEC.md` §5 records the
decision and adds `people`/`contacts` to the modeled facets.

**Acceptance:** a test asserts the assembled prompt carries the section, the call
and the suggested facets — the assembled prompt, which is what the model reads,
not the helper. **Done.**

### T44 — a stopped turn keeps its ledger, and errors travel fenced · `loop/engine.py`, `surface/discord/bot.py` · `done`

**What was missing.** The 22:52 turn lost its evidence twice: the live work trace
deleted itself at every turn's end, so "I got stuck repeating the same
failing step" stood alone above a channel the owner had already watched answer
itself; and the canned reply interpolated the raw tool error, which Discord's
markdown italicised — the pasted error read `"0 7 * * "` where a five-field cron
had been.

**Done.** A turn the engine stops (`finish(error=…)` — repeated failure, step
limit, denial) asks the surface to hold its ledger: `hold_trace()` on
`DiscordSurface`, duck-typed by the engine exactly as `progress` is. The held
ledger takes one final complete edit (a line landing inside the 2.5 s edit interval
is flushed, not dropped — that is the line the owner most needs) and stays. An
answered turn keeps the clean-channel contract: ledger deleted, reply alone. The ✗
trace line's error excerpt widened 80 → 200 characters. The canned reply now
carries the error fenced, with any fence inside the text broken by a zero-width
space.

**Acceptance:** three surface tests — the held ledger stays and ends complete, an
answered turn still clears, a line inside the edit interval survives the hold; two
engine tests — a stopped turn's reply is fenced and the surface was asked (`held
== 1`), an answered one never asks (`held == 0`). **Done.**

**Symptom.** `codeforces_contest_alert` failed with `[Errno 13] Permission denied` on
`run.py`, exit code 127. `disk_free_check` had the identical defect and had not fired yet
(it runs Mon–Fri 07:41, so it would have failed on its next weekday run).

**Cause.** `limits.py` line 53 does `os.execvp(argv[0], argv)` where `argv[0]` is the
entry file — `run.py`. `execvp` execs the file *directly*, which requires the executable
bit. `authoring.py` line 77 writes the file with `write_text()`, which creates it `0644`
— never `+x`. So **every capability authored this way is broken**, not just these two.

**Why it stayed silent.** The runner reports `exit_code=127` with the message on stderr,
but the scheduler treats a non-zero exit as "nothing to notify" — so the job quietly
stops running. A capability that fails on its first run looks exactly like a capability
with nothing to say. This is the part worth fixing hardest: the failure mode is
indistinguishable from success.

**Fix.** Two changes, and the second is the one that matters:

1. `chmod 0o755` after `write_text()` in `authoring.py`, so a capability is not born
   broken. (Done by hand on the live instance for both existing capabilities; both now
   run clean.)
2. A non-zero exit from a scheduled capability should notify, not vanish. A job that
   dies is news; a job that has nothing to report is not. Today they produce the same
   silence.

**Acceptance:** author a new capability, run it on schedule, and it executes without a
manual `chmod`. A capability whose `run.py` exits non-zero produces a notification naming
the capability and the exit code.
