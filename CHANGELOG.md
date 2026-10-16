# Changelog

All notable changes to **Daedalus** are documented in this file.

- **Format:** [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
- **Versioning:** [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
- **Maintenance policy:** every user-visible change gets an entry under `Unreleased` as it lands. On release, `Unreleased` moves under a dated version heading. Entries are **verbose by policy** — record *what* changed, *why*, and *what it supersedes*, so the file is a decision record, not just a diff of names.

Change types: `Added` · `Changed` · `Deprecated` · `Removed` · `Fixed` · `Security`.

---

## [Unreleased]

_Design frozen; **P0, P1 and P2 are implemented as code** — the loop, the primitives, the capability substrate, memory (now **P1 depth**: file-first source of truth, supersession + bitemporal, a `local_only` egress gate, Obsidian-compatible notes), the scheduler and the **P2 proactive gate** (quiet hours, a daily cap, and the freedom to stay silent), the Melinoë gate, the Discord surface, and the setup wizard. The **internal calendar now stands on its own** at gcal parity — recurrence with exceptions and moved instances, time zones that survive a clock change, free/busy and conflict detection, search, bulk edits, reminders that fire through the scheduler, and an `.ics` import for the owner's real history. Verified by `daedalus selftest` (12/12 offline; 13/13 with `--live`) and a 667-test suite. The owner's own free-text request — watch free space on `/` every weekday morning and speak up under 25% — **completes end-to-end**: a capability authored, run once by the Melinoë gate, registered, and scheduled, with no code touched. `0.1.0` is cut once that same round-trip is confirmed from Discord.

### Added

- **The dashboard's controls go through the approval split — and can't go around it.** SPEC §3.6
  has always drawn the line as *"visual → dashboard, action → Discord"*, and until now the dashboard
  was all visual and no action: `POST /action` existed in the routing table and answered *"not built
  yet"*. Now the page carries a control strip — add, move, remove — and every control POSTs to
  `/action`, where `genui/actions.py` resolves it to a **real `calendar` tool call** and puts it
  through the same `approvals.authorize` the loop's own tool calls get: the tool's own risk tier,
  its own scopes, its own assessment. That is the whole point of the seam — if the dashboard had
  its own idea of what a delete may do, there would be two approval policies and the weaker one
  would win by being the one a browser could reach. Five decisions worth their words:

  - **A closed vocabulary, composed by code.** The page may propose exactly
    `calendar.create`, `calendar.update` and `calendar.delete` — nothing else, and no other tool.
    The strip is built in `dashboard.py` *outside* the spec, so a page the model authored carries no
    actions at all: a prompt-injected page cannot render a disguised delete button, because controls
    are not part of any vocabulary the model sees.
  - **Waiting is not hanging.** A control that needs the owner answers **202 waiting** immediately
    and the buttons are surfaced on Discord from the daemon's loop; when they are answered, the tool
    runs there and the outcome is sent to the owner. An HTTP request must never sit open for the ten
    minutes a person takes to decide. With no daemon attached (a bare `daedalus serve`), a control
    that would need approval is refused with why — not silently allowed.
  - **One bootstrap, then a cookie.** A browser form cannot set a header, so the per-install token
    has to reach the browser somehow — but a secret in a rendered document is exactly what the
    GenUI split keeps out. `GET /dashboard?t=<token>` verifies it once, hands the browser an
    `HttpOnly; SameSite=Strict` cookie, and redirects to the clean URL. Only a verified session is
    served controls; every other view is read-only, the token appears in **no** document (asserted),
    and the request log strips the query string so the bootstrap URL is not remembered in it.
  - **Two CSPs, one difference.** A model-composed page keeps `form-action 'none'` — a model's form
    is a dead button. The code-built strip is served under `form-action 'self'`: as far as *this
    server*, and no script source either. The server sends whichever policy the page claims as a
    header, so the meta and the header never disagree.
  - **`daedalus serve` stays a viewer.** The controls need both halves of the split — the
    middleware, and a surface to put a button on — and both live with the daemon, so `daedalus run`
    starts the dashboard with controls attached and `serve` says plainly that it is read-only.

  Writing the picker exposed a **calendar bug the page would have made dangerous**: the tool
  defaulted `scope` to `all` whenever it was unset, so `delete` with a `recurrence_id` — the
  address `agenda` prints for one instance — silently removed the **whole series**, and `update`
  edited it. Naming an instance now means **that instance**, and `scope=all` beside a
  `recurrence_id` is refused as the contradiction it is. 34 tests in `tests/test_actions.py`,
  including the acceptance itself: the same call driven through the tool's path and the page's,
  verdicts compared — with an auto-tier tool both allow, and a block-tier tool both refuse.

- **The active seat now means something — and one CLI verb switches it.** `active = true` has sat
  in the charter files since T11 and been read by nobody: `load_charters` refused two claimants and
  stood up Daedalus when none claimed the owner, and then every turn ran as the default regardless.
  So the owner could write `active = true` on `athena.toml`, start talking, and get Daedalus — with
  the prompt building and the startup clean, which is the failure nobody catches. Now the flag is
  the identity a turn runs as, resolved **once** at the top of `run_turn`
  (`pick(seats, seat)`), so the session, the charter in the prompt, and the memory namespace cannot
  disagree about who is answering. Three decisions:

  - **No name means the active seat, not a hard-coded key.** `pick(charters, None)` returns
    whichever charter carries `active`, and the default Daedalus stands until the owner sets
    another — so the common case is unchanged and the new case is reachable.
  - **Switching is one edit, not two.** `daedalus activate <seat>` writes *every* charter file whose
    flag changes, because activating one seat means deactivating another, and leaving two claims in
    place would be refused by `load_charters` on the very next startup. The flag is still the only
    source of truth — the command writes files and holds no state of its own, so editing
    `active = true` by hand is the same act and does the same thing. A name that matches no charter
    is refused there (`UnknownSeat`) and nothing is written, because silently activating the wrong
    seat is exactly the outcome worth preventing; in a *turn*, by contrast, a name nobody answers to
    still lands on the active seat, since a request is not worth stopping for a phrase.
  - **`resolve()` is the strict lookup, `pick()` the forgiving one.** `pick` is for a turn;
    `resolve` is for the places where a name is a command, and raises. `pick` now matches by key or
    by the name the owner sees, so `daedalus activate Melinoë` works.

  A wake speaks as the active seat too — the agent reaches out as the identity the owner talks to,
  not as a second one. `daedalus seats` marks the active one and says how to switch.

- **Each seat remembers its own things — memory is namespaced now.** SPEC §7 has said since
  the start that a seat is *identity + charter + memory namespace*, and T11 shipped the first
  two; the `namespace` field existed and went nowhere, so every seat would have read the same
  flat memory and one identity's facts would have surfaced in another's brief the moment a
  second seat was switched on. Memory is now scoped by the seat a turn runs as. Four decisions:

  - **The namespace is a directory, not a column.** `memory/facets/` is the default namespace
    (`daedalus`), and any other seat reads `memory/namespaces/<key>/facets/`. Putting it in the
    tree rather than in a metadata field means the filesystem *is* the list of namespaces (new
    `memory/namespaces.py`), a hand-made directory is picked up without a roster, and moving
    a note between seats is `mv` — which is the honest behaviour for a file-first store, because
    the file wins and the directory says whose it is.
  - **The default namespace is the tree that was already there.** `memory/facets/` is
    `daedalus`, so every fact the owner has told Daedalus keeps behaving exactly as it did and
    nothing is migrated, copied, or rewritten on upgrade. The one namespace that may not be
    *moved* is the coordinator's — a charter that tried would orphan every existing fact, so it
    is refused at load with that reason.
  - **The index carries the namespace, and rebuilds instead of migrating.** The FTS5 cache
    gained a `namespace` column, so `REQUIRED_COLUMNS` now includes it: an install whose index
    predates this ticket **fails the column check and is rebuilt from the notes**, rather than
    being read with a guess. `rebuild_index` walks *every* namespace, because a rebuild scoped
    to the handle that triggered it would empty every other seat's memory — the failure would be
    a seat that forgot everything the first time anyone else's index was repaired.
  - **`Runtime.memory` is a property, not a field.** It resolves to the active seat's handle, so
    a tool call cannot reach the wrong namespace by forgetting an argument, and `Runtime.memory_root`
    is the field that names the whole tree. `build_system_prompt` resolves its memory from the
    seat it was *passed* rather than from the ambient session, so a prompt stays a function of
    its arguments. `memory_for`/`namespace_for` in the new `cabinet/namespace.py` are the only
    place a seat becomes a namespace.
  - Seat names are directory names, so they are validated (`^[a-z0-9][a-z0-9_-]*$`) and a name
    that could escape the tree is refused with the path it came from. `pick()` now matches a seat
    by key **or** display name, so the owner can say `Melinoë` and get `melinoe`.

- **The seat charters are the owner's files now.** The eight charters lived as string literals in
  `cabinet/seat.py`, which made "not that — more like this" an edit to Python. They are now files at
  `$DAEDALUS_HOME/cabinet/seats/<key>.toml` — `name`, `charter`, and optionally `namespace`, `active`
  and `traits` — written out on first load and read at startup, so an edit to a file changes what that
  seat answers from on the next turn. The literals remain as the **fallback**: a file that is deleted
  falls back to its built-in rather than to nothing, which makes "never mind" something the owner can
  say by removing a file, and means a fresh install needs no setup step. A file the loader cannot
  believe — invalid TOML, no charter, a field it does not know — **stops startup with the path and the
  problem**. The failure it prevents is the one the owner would never catch: the built-in answering
  quietly in place of the charter they wrote, with the prompt still building and still saying the old
  thing. A typo'd `charterr` is refused for exactly that reason. Exactly one seat is active; two
  charters claiming the owner is refused (which one is answering?), and none leaves the default in
  place (a turn always has an identity). `daedalus seats` lists them with the charter each is reading.
  The `Runtime` now carries the loaded seats, and `build_system_prompt` reads the charter from there —
  which is what makes this a change to behaviour rather than to storage.

- **The calendar is a page now — the day, the week, the month.** SPEC §5.5 asked for *"a full
  dashboard view (`serve`) — day / week / month — used the way gcal is used"*, and `serve` was
  serving a directory of files with nothing in it. `genui/dashboard.py` reads the internal store and
  composes the three views; `genui/server.py` answers `/dashboard` (today) and `/day/`, `/week/`,
  `/month/` (each optionally pinned to a date, so a rendered cell is linkable). `/dashboard` leaves
  the routing table's *"not built yet"* list — it is built — while the four writes the approval
  split owns (T8, T9) keep saying what they will be. Four decisions are worth the words:

  - **Occurrences, never definitions.** Each day is read with the store's own day addressing, so a
    repeating event appears on *every* day it falls and an instance the owner moved appears where
    they moved it. A view built from definitions is correct on the day a rule began and wrong on
    every other day of the year — which is the failure a calendar hides best, because the page still
    looks like a calendar.
  - **Monday → Sunday, and a month drawn as whole weeks.** A month grid whose first row is four
    cells wide is a list, not a calendar. Days that spill in from the neighbouring months are drawn
    with their date and nothing beside them: they exist, and they are not this month's.
  - **The spec carries keys; the values are read here.** The page is composed the way a
    model-composed page is — `Spec.parse` without values, bound at render time — so
    `page.spec.to_dict()` holds no event summary and is safe to show, log, or hand back. That is
    §3.6's *structure travels, values stay local* kept true on a page nothing remote will ever see,
    and it is what lets a generative dashboard reuse the vocabulary later without the owner's data
    ever entering a prompt. The self-test asserts the two halves separately, because a test that
    only proves the page *contains* the event proves nothing about the spec.
  - **`local_only` is shown here.** §6 withholds from **egress**. This page is the owner's own
    surface on the owner's own loopback with no model in the loop — hiding the owner's own
    appointment from the owner's own calendar would be the rule turned inside out.

  Each render is also written to `$DAEDALUS_HOME/ui/` (`day-2026-10-16.html`, and today's view
  becomes `index.html`), so the artifact outlives the request that made it; `daedalus serve` draws
  today's page once at startup so `/` opens on the calendar rather than on a file listing. A date
  the store cannot read is refused with the format it wanted rather than defaulted to today —
  *"I asked for the 16th and got today"* is a page that looks right, which makes it worse than an
  error. A server built without a calendar says exactly that, rather than claiming to be unbuilt.

- **The self-test renders a dashboard, because the spec says it must.** SPEC §8.5 lists *"dashboard
  render"* among the checks that end provisioning, and it was the one item on that list nothing
  performed. The twelfth check now writes an event to the scratch calendar, reads it back as an
  **occurrence** the way every other read does, binds it into a rendered day view, and insists the
  marker is on the page — in a document that names no remote origin, carries a policy, and never had
  the value in the model-visible spec. The two failures it exists to catch are the ones a rendered
  page hides perfectly: one that reaches for something on a network, and one that renders *around*
  the owner's data. A tidy empty grid looks exactly like a working dashboard.

- **A set of events can be addressed by the day it falls on, and moved by a date delta.** A calendar
  the owner talks to has to hear *"the coming weekend Sunday"*, and the two things that fall on a day
  need not share a word to match on. `calendar op=update`/`op=delete` now take `on="2026-10-25"`: the
  events that **produce an occurrence** that local day, alone or narrowed by a `match`. On its own
  the day is the whole address, which is what "move the two things on Sunday" needs when the things
  are called different names.
  - **`shift="-7d"` moves the set by a *date*, keeping each event's own time and length.** `at` sets a
    time of day on each event's own date and `start` names one shared date — neither can say "a week
    earlier", and handing the same date to a set would pile it onto that day. A signed day or week
    offset (`"-7d"`, `"2w"`, `"3d"`) is that missing shape, and unlike `at` it moves an all-day event
    too, because days are a whole-day event's natural unit. This is the call the owner's real
    correction needed — `op=update on="2026-10-25" shift="-7d"`, one call for two events that share
    no name.
  - **The day-set is gated like every other read.** The matched set passes through the same
    `local_only` egress filter, so on a remote-bound turn the model cannot address an event it was not
    allowed to see; the write still takes the `CAUTION` approval, now worded *"change every event on
    2026-10-25"*.
- **The renderer turns structure into the owner's own page, and fetches nothing.** `genui/renderer.py`
  is the last step of the pipeline — a spec, local values and a mood in, one self-contained HTML
  document out — and it is where the shape of the design finally pays off.
  - **Values are bound locally, and the model never saw them.** The model composes *keys*:
    `{"bind": "calendar.today.events"}` (or `"{{disk.free}}"`, accepted when it is the whole value,
    so text that merely contains braces is never mistaken for one). A local lookup fills them in at
    render time. This is what makes *"structure travels, values stay local"* (§6) **structural**
    rather than a rule someone has to remember: a `local_only` fact reaches the owner's dashboard
    because it was never in the spec at all — not because something remembered to filter it out of
    one. The test asserts both halves at once: the page contains the value, and the spec does not,
    before *and* after rendering.
  - **What rendered is what was validated.** A bound value goes through the *same* door as a literal
    one, so a binding cannot smuggle in a shape the prop would have refused. The guarantee is not
    "the model's spec was checked" — it is "the thing that actually rendered was checked", which is
    the one that matters when the value came out of the calendar. A `local_only` value carrying
    markup is refused for exactly the same reason a model-authored one is.
  - **The page fetches nothing, and the browser is what holds it to that.** No scripts, no external
    stylesheet, no fonts, no images: one document with its stylesheet inline, plus a
    `Content-Security-Policy` of `default-src 'none'`. A promise asserted only by a test is a promise
    the next edit can break; asserted by the browser as well, it cannot silently become a page that
    phones home.
  - **Mood resolution has one order, stated once:** the owner's one word, then the mood inferred from
    local signals, then the mood the model proposed, then `calm`. The model is meant to be generative
    in *styling choices*, so its mood is honoured — but never above the owner, and never above what
    the laptop can tell about the moment.
  - **A binding that never got bound is refused loudly.** Reaching a renderer still bound is a bug,
    and printing `Binding(key=…)` on the owner's dashboard would be a strange way to report it.
  - **`render_fragment()`** returns the blocks with no document around them — which is what a live
    *draft* preview needs when the page is already open.
  - Two supporting additions: `Binding`/`Spec.bind()`/`Spec.has_bindings()` in the spec module, with a
    binding permitted anywhere a value is (a whole prop, one list item, one table cell); and a
    `scheme` token, so a dark mood gets dark scrollbars and form controls instead of a page that reads
    as broken.

- **A mood is a complete, validated token set — never a stylesheet.** SPEC §3.6 asks for a dashboard
  generated *"in the owner's theme, for the owner's mood"* while also forbidding arbitrary
  model-generated CSS. So the theme is not written at render time: it is a **token set the owner
  approved**, and a mood is a *named choice among those sets*. `genui/theming.py` holds five —
  `calm`, `midnight-focus`, `dense-ops`, `low-energy`, `celebration`.
  - **A mood is complete or it does not exist.** Every mood supplies every token, and a hole is a
    refusal rather than a default — because "a default somewhere" is exactly how a theme becomes
    whatever the fallback happened to be.
  - **Every value is validated.** Colours are hex; a length needs a unit (`0` excepted, as CSS
    excepts it); motion is a duration. A font stack is an **allowlist** — no parenthesis is
    permitted, so `url(…)` cannot ride in on a font and turn a rendered page into a fetch.
  - **All CSS lives in one module and reads only declared tokens.** A test parses every
    `var(--d-…)` out of the component sheet and asserts it names a declared token, so a component
    cannot quietly depend on styling that is not part of the owner's vocabulary. Another asserts
    that no mood's sheet contains `url(` or `@import` — self-contained is the promise, and a fetch
    hidden in the theme would break it silently.
  - **Mood is inferred from structured local signals, never from a model:** a celebration outranks a
    flat battery, a flat battery outranks a late hour, a crowded day outranks the default. The hour
    is **passed in** — the caller owns the zone and nothing here reads a clock. A one-word owner
    override wins, and a typo in it falls through to the inferred mood rather than costing the owner
    their theme. An unrecognised mood renders `calm`: the failure mode of a bad mood name should be
    a plain page, not a broken one.

- **Six shapes the dashboard can be made of.** `genui/components/` is the visual vocabulary the spec
  is written in — `stat-card`, `list`, `timeline`, `table`, `prose` and `calendar` — each a **prop
  schema and a renderer**, which is what a component is (SPEC §3.6 calls them *authored
  capabilities*: the set grows by proposal, not by drift).
  - **Components carry no styling.** They emit semantic markup with class names; the mood token set
    supplies the CSS. That split is why one spec renders in any mood without the model knowing
    anything about colour, and why the only place CSS is written is a place the owner approved.
  - **The renderer escapes too.** Every value is escaped *again* at render time, and the tests prove
    it by walking past the validator: they build a `Block` by hand, as a bug or a new component
    would, and assert a `<script>` tag still cannot reach the page. A test that only goes through
    `Spec.parse` can never show this — the door refuses first.
  - **Two new schema powers, both earned here.** `Prop.choices` — a closed set, written down, named
    in the refusal: *`view` is one of: day | week | month; it is 'year'*. And `Component.check` — a
    cross-prop rule that runs **at the door**, so the `table` refusing a row wider than its headings
    and the `calendar` refusing a day that is not `[date, weekday, what is on it]` fail as a refused
    spec rather than as a half-rendered page.
  - **A short table row is padded; a wide one is refused.** Blank cells are visible and harmless;
    inventing a column drops nothing, and truncating drops the owner's value silently — so the
    second is a refusal.
  - **Empty states are said, not drawn.** An empty list or an empty day renders *"Nothing on."*
    rather than an empty `<ul>` — a dashboard that shows a blank rectangle is a dashboard that looks
    broken.
  - **The vocabulary and the validator cannot drift:** one test asserts every schema has a renderer
    and every renderer has a schema, and another builds a minimal valid spec *from the schema* for
    each — so a component added without its other half fails on the way in.

- **The dashboard has a shape, and the model may only fill it in.** SPEC §3.6 asks for a dashboard
  *generated per render* while also promising *"never arbitrary model-generated CSS/JS"*, and the
  only way both can be true is a split: the model composes **structure**, a deterministic renderer
  **disposes**. `genui/spec.py` is where that split is enforced.
  - **A closed vocabulary, checked before anything renders.** A spec is an ordered list of blocks —
    `{"component": "stat-card", "props": {…}}` — validated against the owner's component vocabulary
    *first*. An unknown component, an unknown prop, a missing required one, a wrong type: each is
    refused before a single byte of HTML exists, so a bad proposal never reaches a renderer.
  - **Two defences against markup, not one.** A value carrying a tag is refused at the door **and**
    escaped at render time. The door is what keeps the vocabulary honest; the last mile is what keeps
    one buggy component from becoming an injection. Neither alone is a design — the pair is.
  - **A tag, not a less-than sign.** `dinner <-> party` is a value; `<b>` is markup. The guard is
    specific so the vocabulary stays usable. A `javascript:`/`data:` value is refused outright, and
    the only link a spec may carry is a **relative path on this server** — an absolute URL would make
    a rendered page fetch something the owner never chose.
  - **`true` is not a number.** `isinstance(True, int)` is true in Python, so a JSON `true` would sail
    through a naive numeric check. It is refused, and the trap has its own test.
  - **A page is bounded** — at most 24 blocks, 60 items, 600 characters to a value — so a runaway
    proposal is a refusal rather than a document the phone struggles to open. Lengths are refused,
    never silently truncated; truncation would hide the runaway instead of reporting it.
  - **Refusals name the way out.** Every message says what it *received* and what it would have
    accepted instead: *`'chart'` is not a component. The vocabulary is: …*. A refusal that says only
    "invalid" is how a model ends up retrying the same call — the failure the owner hit in the
    calendar that same morning.
  - **Two spellings accepted on purpose:** `type` for `component`, `components` for `blocks`. Learning
    the name is free; refusing the synonym costs a step and teaches nothing.
  - **`describe()`** renders the vocabulary for the prompt, optional props marked, so the model's
    instruction and the validator cannot drift apart.

- **The laptop can serve what it generated, and only to itself.** `daedalus serve` stands up an
  HTTP server for the dashboard and every other generated artifact. It is built so that the two
  ways it could go wrong are the two things it *cannot* do:
  - **Loopback, and not by choice.** The bind address is `127.0.0.1` — not a parameter, not an
    override, not a `--host`. There is no code path that opens a public Funnel. Publishing it to
    the phone is a **separate** act, with its own ticket (Tailscale Serve), because *open a port*
    and *open a port to the internet* should never be the same keystroke. The gate asserts the
    address the socket **actually** came up on, not the one we intended.
  - **No anonymous controls.** Reads are open to anything already on the loopback — that is what
    loopback means. Writes are not: every control `POST` carries a per-install token from the
    encrypted store, and a server whose token is *unset* accepts **no** writes at all. Failing
    open there would turn "the dashboard" into an unauthenticated remote control for the owner's
    calendar, reachable by anything that can curl. The token is minted on first use and encrypted
    at rest, like every other credential.
  - **The routing table is written down.** `GET /dashboard` and `/draft`, `POST /tune`,
    `/approve`, `/action` are named *before* they are built, so a control that is designed but not
    yet implemented answers **"not built yet"** rather than 404 — the difference between "I have
    not done that" and "that is not a thing", which the owner is entitled to be told apart.
  - **A request cannot climb out of the artifact directory.** Names are matched against a pattern
    before they are joined to a path, and the resolved parent is checked as well, so `..`, an
    encoded `..%2f`, a `sub/../../` walk, and a symlink pointing outward are all refused rather
    than served — with a test for each, because traversal guards are the kind that look fine in
    review and fail in production.
  - **`daedalus serve`** is the new subcommand (`--port`, `0` to take whatever is free), and the
    self-test grows an eleventh check that **starts the server, knocks on it over a real socket,
    and confirms an unauthenticated write is refused** — both properties are silent failures that
    configuration alone cannot see.

- **The owner's real calendar can now come in.** A calendar you cannot bring your history into is
  a calendar you cannot move to, and the owner's calendar already exists — in a Google Takeout
  `.ics`, in a decade of events. `daedalus calendar import <file.ics>` reads one:
  - **A whole document, not a toy.** Many `VEVENT`s at once, `X-WR-CALNAME` provenance, one
    `VTIMEZONE` per zone, `TZID`-qualified times, `RRULE`s, `VALARM`s and `RECURRENCE-ID`
    overrides. The internal store is already valid iCalendar, so it needed a reader for the
    parts of the format it had not been handed yet, not a new format.
  - **Identity is `(uid, recurrence_id)`,** the pair the index already keys on. That is what lets
    an override arrive *with* its series instead of overwriting it — the standup and the one week
    it moved are one import, and dropping the recurrence-id from the key would have silently lost
    every moved instance in the file.
  - **Re-running it is safe.** A second import of the same file adds nothing and rewrites nothing.
    `--replace` overwrites an event that is already here, for when a *newer* export should win.
  - **Provenance that does not lie.** Foreign events keep the name of the calendar they came from
    and are stamped `authority="imported"`. A file this store wrote round-trips with its own
    provenance untouched — the distinction is the document's `PRODID`, not a guess per event.
  - **Zones travel; unmodelled components are counted.** Each `VTIMEZONE` is carried over once, so
    a `TZID` the file names has its rules in the same file. A `VTODO` or `VJOURNAL` is *not*
    copied — nothing models it — and the report says how many were passed over rather than
    swallowing them or pretending they were events.
  - **`--local-only`** marks the whole import withheld from remote-bound prompts: a history the
    owner wants to search but not quote (§6).
  - **A command, not a tool.** The `calendar` primitive is scoped to `state`; an import reads an
    arbitrary path, so it stays in the owner's hands. The agent never goes looking for a file to
    swallow.

- **A change can reach a whole set, not one row at a time.** Until now every edit named a single
  event by its id, so "move all my standups to 10" was impossible to *say* — the model had to find
  each one and issue a call per event, which is both slow and a chance to guess an id wrong.
  `calendar op=update` and `op=delete` now take `match=<words>` in place of `uid`:
  - **Matched on the name, not the notes.** `match="standup"` means the events *called* standup. An
    unrelated meeting whose description happens to mention one is not swept along — a bulk change
    catching the wrong thing is a surprise in the worst place.
  - **`at="10:00"`, not `start=...`.** A bulk retime applies a *time of day* to each event's own
    date, keeping its length. Handing every match the same full `start` would collapse a week of
    standups onto a single morning, so the tool refuses that shape and says which one to use
    instead rather than doing something plausible and wrong.
  - **All-day events are left out of a retime.** A whole-day event has no hour to move, and quietly
    converting one into a timed event is not a small edit.
  - **The approval prompt names the set.** "Remove every event whose name matches 'standup'" — the
    owner sees the *shape* of the change, not just `calendar delete`.
  - Renaming, retiming, relocating, re-ruling and re-reminding all work on the set; a `match` that
    finds nothing is reported as nothing, not as success.

- **A reminder is a message, and it now arrives.** A `VALARM` was round-tripped but nothing
  ever acted on it, which made it decoration. The scheduler tick now looks back over its own
  window for reminders that have come due and delivers them, so \"remind me 15 minutes before
  the dentist\" is a thing the owner can ask for and then *receive*:
  - **Set from either side.** `calendar op=create`/`op=update` take `remind=<minutes before>`,
    and a hand-written `VALARM` in `events.ics` (including one gcal wrote) is read as the
    offset it already was. `TRIGGER:-PT1H30M` is \"1h 30m before\"; the sign is stored as a
    person thinks it — positive means *before* the start.
  - **Exactly once.** `state/reminders.json` records which nudges have been delivered, so a
    tick that runs twice in a minute — or a daemon restarted mid-minute — does not nudge the
    owner twice about the same meeting. Entries are pruned after a week.
  - **Not behind the proactive gate.** A wake is the agent *choosing* to speak; a reminder is
    an instruction the owner left behind. So quiet hours and the daily cap — which bound the
    agent's own cadence — have no say: a 03:00 alarm the owner set fires at 03:00, and the
    nudge does not spend the day's allowance. Same reasoning that already kept capability
    alerts ungated, now written down where the gate lives.
  - **A late tick still delivers.** The look-back is 15 minutes, deliberately longer than the
    tick period so a reminder falling between two ticks is not lost, and deliberately not
    longer, so a daemon that was down overnight does not wake the owner with a pile of
    meetings that have already happened.

- **The calendar answers questions about time, and knows which clock it is on.** A list is not
  a calendar; being able to say *when the owner is not free* is. New read ops, all over
  occurrences rather than definitions:
  - **`busy`** — the merged stretches the owner is occupied, so five overlapping events read
    as the one blocked stretch they are. Anything marked `TRANSP:TRANSPARENT` is on the
    calendar without blocking it (a "Focus" event does not make you unavailable), and two
    events claiming the same minutes are reported as the **double-booking** they are rather
    than silently merged into a stretch the owner would read as a single meeting.
  - **`free`** — the gaps of at least N minutes inside each day's waking hours (default
    09:00–18:00; `hours="any"` opens the day). Bounded on purpose: a schedule that cheerfully
    offers 03:00 is technically correct and practically useless.
  - **`search`** — events found by their own text over an FTS5 index (the same shape memory
    uses), falling back to `LIKE` if FTS5 is unavailable rather than reporting "nothing found"
    about something that plainly exists. It returns the *series*, not one row per instance.
  - **Clocks other than this one.** `TZID` now *means* something: the zone travels with the
    time, `recur.py` stays pure wall-clock arithmetic, and real instants — not wall-time
    strings — are what free/busy and overlap compare. So a 09:00 call in London is 09:00 in
    London either side of the clocks changing, lands on the right day here, and says "London"
    on the line only when it is not this machine's own clock. An unreadable zone falls back to
    floating rather than guessing an offset, because a wrong hour delivered confidently is
    worse than one that is merely local.

- **The internal calendar learned to repeat — and to answer for the day.** A calendar that
  can only hold one-off events is a list; the thing that makes one *useful* is being able to
  say "every weekday at 9:10" and then ask "what is on Thursday". So a repeating event is now
  **one event with an `rrule`**, and reads expand it into the occurrences that actually
  happen:
  - **`RRULE`, expanded properly.** `DAILY` / `WEEKLY` / `MONTHLY` / `YEARLY`, with `INTERVAL`,
    `COUNT`, `UNTIL`, `BYDAY` (including ordinals — `2TU`, `-1FR`), `BYMONTHDAY`, `BYMONTH`,
    `BYSETPOS` and `WKST`. The expansion is period-based, the way RFC 5545 describes it, so
    `FREQ=MONTHLY;BYDAY=MO` means *every Monday of the month* and the last weekday of the
    month is `BYSETPOS=-1`, not a coincidence. `EXDATE` removes instances; a
    `RECURRENCE-ID` override is a second `VEVENT` sharing the series' `UID` and replaces
    exactly one instance. Sub-daily frequencies are **refused, not approximated** — a wrong
    answer delivered confidently is worse than a reported gap.
  - **Reads answer with occurrences; writes say what they mean.** `agenda` shows a repeating
    event on each day it falls; `list` shows the definitions; an instance is addressed as
    `uid@its-own-start`. `scope=all` edits the series, `scope=this` moves one instance,
    `scope=future` splits the series at that point — the difference between "the standup
    moved" and "today's standup moved", which a tool that could only do one of them would
    silently get wrong.
  - **Reminders and busy-time are modelled.** A `VALARM` is kept as an offset from the start
    (`TRIGGER:-PT10M` → "10m before"), and `TRANSP` records whether an event makes the owner
    busy. A `VALARM`'s own `DESCRIPTION` no longer overwrites the event's — a nested-component
    bug that would have quietly replaced "Bring the referral" with "Reminder!".
  - **A zone travels with the time.** `DTSTART;TZID=Europe/London:…` round-trips instead of
    being flattened to a bare floating time, and an all-day span (`2026-10-15` → `2026-10-18`)
    now shows on every day it covers rather than only its first.
  - The index became `(uid, recurrence_id)`-keyed — an override shares its series' `uid`, and
    a table keyed on `uid` alone would let one instance evict the rest. When the shape changed,
    the cache **rebuilds** rather than migrating: it is derived from the `.ics`, which remains
    the truth. `daedalus selftest` proves the round-trip (a rule, a moved instance) rather than
    just a write.

- **P2 — the proactive scheduler.** The agent can speak first now — and, just as
  importantly, not. Three things had to be true for that to be safe, and each is a
  separate decision:
  - **Whether it may speak is decided in code, never by the model** (SPEC §6).
    `scheduler/proactive.py` gates a wake on *quiet hours* (default 22:30–07:30, with
    an overnight window handled as an overnight window) and a *daily cap* (default 4).
    The gate is consulted **before** the model is reached at all — an agent that can
    argue its way past its own quiet hours does not have any. Both refusals are
    recorded with their reason, so "why was it quiet" has an answer that is not a guess.
  - **A wake composes fresh, but it is not amnesiac.** `Runtime.briefing()` hands the
    model the time, today's agenda, how often it has already spoken, and what it said
    last — and tells it not to repeat that. "The daily message is not a template"
    (SPEC §9) means the model writes it; it does not mean the model writes it blind.
  - **It may look at the day and stay silent.** The model answers `[silent]`, or
    nothing, and no message is sent and no quota is spent. Silence is a *decision*
    here, not the no-output fault that a reply to the owner treats it as — which is
    why the two paths are separate rather than sharing one fallback.
  A **capability alert is not a wake and is not gated**: the owner defined that
  capability and asked it to speak up, and silencing an alert at 03:00 would defeat
  the thing it was built for. The ledger is file-first `state/proactive.json`, so the
  day's decisions can be read by hand.
  **No wake job ships.** The gate and the composition path are built and nothing uses
  them until the owner asks for a cadence — an agent that starts talking before it has
  been asked to is a decision, not an implementation detail, and it has not been made.
  - **A wake withholds `local_only` values.** The briefing is composed before the wake's
    model chain is resolved, so it has no honest way to know where it is going — it
    withholds, and it decides that on its own account rather than by reading the session.
    Otherwise a DM that happened to route through a local model would hand the next wake
    permission to carry `local_only` values into a prompt that may well be remote.
  - **The prompt says so.** Free text is the only interface, so a wake path the agent is
    never told about is not built. The system prompt now states that the agent may speak
    first, that quiet hours and a cap are enforced around it, and that saying nothing is
    usually the right answer — otherwise the gate would be protecting a silence nobody
    could tell apart from a feature that was never wired up.
- **`daedalus wake`** — probe what the proactive path would say right now, without
  sending it and without spending the day's allowance: the message, or the reason it
  stayed quiet. Checking must not change what is being checked.

- **The internal calendar — a store the owner actually owns.** `$DAEDALUS_HOME/calendar/`
  now holds a real calendar: `events.ics` written as valid iCalendar (RFC 5545), an
  `index.sqlite` derived from it, and a `calendar` primitive to reach it (`agenda`,
  `list`, `get`, `create`, `update`, `delete`). Two things make it *file-first* rather
  than merely file-*formatted*:
  - **Every mutation re-reads the file before changing it.** `add`/`update`/`delete` load
    `events.ics`, apply the change, and write the whole document back — so an event the
    owner added by hand, a property this module does not model (`RRULE`, `VALARM`), and a
    `VTIMEZONE` block all survive a tool write instead of being flattened by a private copy
    of the truth. The index records the file's revision at nanosecond resolution and
    rebuilds whenever it moves, so a hand edit is never shadowed by a stale cache — a
    coarse `mtime` comparison would have missed an edit landing in the same second as the
    last write.
  - **The shape of a time decides the kind of event.** `2026-10-16` is all-day;
    `2026-10-16T09:10` is a moment. There is no separate flag to contradict it.
  Events carry a **data class**, so a `local_only` event is withheld from any prompt that
  could reach a remote model — the same gate memory has — and the calendar is a **context
  signal** in the system prompt, so the agent knows the day without being asked. `gcal`
  remains **unbuilt**: `sync_state.json` is an empty cursor slot, and the tool never reaches
  another calendar. *Supersedes:* `calendar/` as a declared-but-empty directory.

- **P1 — memory depth.** P0 shipped memory v0 (write a fact, recall a fact). P1 makes
  it a memory that can be *corrected* and *trusted*:
  - **The markdown is the source of truth; the index is a derived cache.**
    `memory/facets/<facet>.md` holds every fact; `memory/index.sqlite` is an FTS5
    cache that `MemoryStore.rebuild_index()` reconstructs from the notes at any time.
    A table written before the bitemporal columns existed (FTS5 has no `ALTER TABLE`)
    is rebuilt from the notes on open rather than silently losing recall. An owner
    editing a facet by hand is no longer fighting a hidden second copy of the truth.
  - **Supersession actually persists.** It was a no-op: `MemoryStore.get()` looked its
    id up through the FTS index, where `id` is `UNINDEXED`, so it never found the row,
    and `supersede()` mutated a detached object and wrote nothing. Entries now carry
    `valid_from` / `valid_to` / `superseded_by`, `get()` reads by id, and superseding
    a fact closes its validity in both the index and the note. **Recall returns what
    is true now**; `include_invalid=True` (a new `memory op=recall` flag) returns
    history.
  - **`memory op=supersede`** — the model can retire a fact it can no longer stand
    behind, given the old entry's id and the fact that replaces it.
  - **Obsidian bridge.** A facet is now a valid vault note: YAML frontmatter
    (`facet:`, `tags: daedalus/memory/<facet>`) and one list item per fact, its
    metadata in a hidden HTML comment. A bullet written by hand is adopted into the
    index with a **stable** id (a content hash), so it does not mint a new identity
    on every rebuild.
  - **`memory op=write` takes `data_class`** — the owner or model can mark a fact
    `local_only`, which then never leaves the machine (see *Security*).
- **`memory/markdown.py`** — the renderer/parser that makes the file canonical:
  `render_entry`, `parse_facet`, `stable_id`, `normalise_content`. Facts are
  single-line; whitespace is collapsed on write so an entry round-trips exactly.
- **Tests** — the suite is now **192** (from 112). New coverage: supersession retires
  a fact and survives an index rebuild, both timestamps round-trip, the index rebuilds
  from hand-written markdown with stable ids, a facet is a valid Obsidian note, the
  `memory` tool can supersede, `local_only` memory is withheld from a remote chain but
  reaches a fully-local one (both in the prompt and through the tool channel), every
  default provider carries a stall ceiling, and each capability gets its own venv.
  The approval prompt is pinned too: the title names the real operation, a malformed
  call says so and drops the irreversible-delete warning, the buttons carry the action's
  labels, and a denial stops the turn instead of asking again. The calendar is pinned the
  way the store claims to work: an event round-trips through the `.ics`, a hand-added
  event and an unmodelled `RRULE` survive the next tool write, a `VTIMEZONE` block is not
  deleted, a same-second hand edit is not hidden by the cache, structural characters
  (`;`, `,`, newline) escape and unescape, and a `local_only` event reaches neither the
  prompt nor the tool channel. `browser` is pinned against a stub Playwright, so the
  no-selector path that used to raise is now a test that needs no Chromium. Proactive
  speech is tested against a fixed clock rather than the hour the suite happens to run
  at: an overnight quiet window at both edges, a window inside one day, an unset /
  unreadable / zero-length window, the cap and its reset at midnight, a corrupt ledger
  that must not disarm the gate, a wake that speaks and is counted, one that is capped,
  one that chooses silence and spends nothing, one that proves a wake cannot inherit a
  previous turn's `local_only` permission, one that proves the prompt actually teaches
  the wake path, and an alert that quiet hours must not touch. The
  self-test's memory check now proves supersession, the rebuild, and the `local_only`
  gate, and a calendar check proves the same three properties, so the acceptance gate
  exercises P1 rather than just v0.

- **P0 runtime** — the loop and the substrate, as code:
  - **Package** — `src/daedalus/` with `loop/`, `model/`, `tools/`, `approvals/`, `capabilities/`, `resources/`, `memory/`, `scheduler/`, `surface/`, `cabinet/`, `security/`, `storage/`, plus `selftest.py` and `main.py`. CLI: `daedalus version | selftest | ask | run`.
  - **Turn engine** — context assembly (charter + capability inventory + resource inventory + recent memory + tool schemas + standing grants) → model → multi-step tool loop → reply, with per-channel history and a hard step limit so a runaway turn stops itself.
  - **Model client** — one OpenAI-compatible client over a provider chain, with a cross-process SQLite rate-limit ledger, a cheap local route, and SSE streaming; it falls through the chain on any provider failure.
  - **Primitives (Layer 1)** — `files`, `shell`, `code`, `http`, `search`, `browser`, `process`, `memory`, `schedule`, `discord`, `ask`, `secrets`, and `capability`. A **workspace guard** refuses protected paths (`.env`, SSH keys, private docs) and anything outside the permitted roots.
  - **Approval middleware** — risk tiers, **host-scoped** standing grants, the sink check, and an append-only decision log. Egress asks once, then becomes a grant; a grant for one host never covers another.
  - **Capability substrate (Layer 2)** — `capabilities/<name>/{run.py, manifest.toml, README.md, state/}`, a per-capability uv venv, the JSON-in / JSON-out run protocol with a timeout, and the **Melinoë gate**: a newly authored capability is run once and checked (exit, output shape, scope posture) before it may be scheduled. An unverified capability cannot be scheduled unattended.
  - **Memory v0** — file-first facet markdown + an FTS5 index, carrying provenance, confidence, and supersession.
  - **Scheduler v0** — a dependency-free 5-field cron matcher, file-first jobs, a wake log with **idempotency keys** (a repeat tick or a sleep/resume cannot double-fire), and a tick that runs authored capabilities with **no model in the loop**.
  - **Surfaces** — a Discord surface (one bot, DM-first, allowlist-enforced, approval buttons, chunked replies), plus CLI and scripted doubles for local use and for tests.
  - **Secrets** — an encrypted credential store (Fernet, per-install master key at `0600`) and a **bytes-stay-home** guard that blocks any outbound request whose body carries a provisioned secret value.
  - **Self-test** — `daedalus selftest` runs layout · secrets · memory · approvals · the Melinoë gate · the scheduler tick · a full free-text turn · and **the daemon lifecycle**, all offline with a scripted model; `--live` additionally walks the configured provider chain and reports which entry answered.
  - **Setup wizard (code)** — `scripts/setup.sh`: the `template.sh` library **vendored verbatim** from the `wizard` skill (every line from `set -euo pipefail` through the `STAGES` marker byte-identical), with the 13 stages authored below it, plus `-y/--yes` and `--non-interactive` modes. Deliberate deviations from the §8.5 stage list, each because the runtime disagreed with the prose: the secret-store **directory** is `0700` (a directory needs `+x` to be traversable; the files inside stay `0600`), the master key sits at `resources/master.key` because that is where `SecretStore` reads it, stage 10 installs only the two named units (the runtime pumps ticks **in-process**, so the timer triggers `daedalus.service` with `Persistent=true` rather than inventing a `tick` verb), and stage 12 runs `selftest --live` when a heavy key is configured. Nothing from the wizard is written to CI — no secret leaves the machine.
  - **Tests** — 112 tests across cron, approvals, the danger classifier, capabilities, memory, the engine loop, the tool schemas, scheduled notifications, the files guard, the secret store, the daemon lifecycle, the Discord surface, and the self-test's own isolation guarantee.
  - **`tests/test_notifications.py`** — the last hop, tested. A scheduled capability that nobody is told about is not automation, and the run has no model in it to notice. These tests drive a real tick: the capability's notification reaches the surface (its own words intact, titled with the name the owner chose rather than the internal `cap-` job handle), a repeat tick for the same minute does **not** message twice, a run with an empty `notifications` list stays silent, and `state` carries between scheduled runs.
  - **A trace of the loop, in the log.** The engine logs every tool call — the tool, its `op` when it has one, and whether it succeeded, failed, or was blocked, with a short reason on failure and a character count on success. **Arguments are never logged**, because they carry the owner's words; the trace records shape and outcome only. This exists because the failed acceptance run was invisible: sixteen identical tool calls left not one line behind, and the only way to see what the model had actually sent was to re-drive the turn by hand through a scripted probe.
  - **`tests/test_tool_schemas.py`** — the schema audit as a regression test. It fails if any tool offers a payload parameter (`content`, `run_py`, `query`, `command`, …) as optional, naming every offender at once. It caught two more instances on its first run (`process.command`, `memory.query`) that the live failure had not yet exposed.
  - **A danger classifier for approval** — `approvals/classify.py`. The owner reported that an approval reading `Approval needed — shell on shell:*` told them nothing: not what the command was, not what it did, and not why they were being asked. It also treated `git status --short` and `rm -rf ~` as the same event, so a prompt carried no information either way. Every call is now judged on a four-level scale by **how bad its worst plausible outcome is** — `ok` (reads state) · `caution` (changes something, reversibly) · `destructive` (destroys data or history) · `catastrophic` (can take the machine with it, or reaches a path the owner put out of bounds) — and that judgement drives the risk tier: `ok → auto`, `caution | destructive → ask`, `catastrophic → block`. The rules cover fork bombs, `rm` on a root/home/wildcard or one level inside a system root, `mkfs`/`fdisk`/`parted`, `dd of=/dev/*`, redirects into a block device, `git reset --hard` / `clean -fd` / `push --force` / `branch -D`, pipe-to-shell, recursive `chmod`/`chown`, shutdown, `kill -9`, `sudo`, service control, package installs, and writes — while a deliberate read-only allow-list (`ls`, `cat`, `df`, `rg`, `git status|log|diff`, `systemctl is-active`, …) keeps ordinary inspection from asking at all. It is **deterministic on purpose**: an approval decision must never wait on a model round-trip, and a model's opinion of its own safety is exactly what SPEC §6 says never to trust. Both error directions have a cost, so a command that matches nothing is **not** called safe — it is reported as unrecognised and asks.
  - **`Tool.title` and `Tool.assess`** — a tool now describes its own call. `title` is the human name (`"Run a shell command"`, never `shell`); `assess(args)` returns the level, a one-line summary of what the call does, and the reasons it was flagged. `shell` judges the actual command text; `capability` speaks per operation (`author` / `run` / `verify` / `schedule` / `remove`); `code`, `discord`, `http`, `search`, `browser`, `secrets`, `schedule`, and `process` each state their own case. The default follows the tool's own risk tier — a tool that cannot describe itself does not get to claim it is read-only.
  - **`tests/test_classify.py`** — 47 tests over the two halves of this. The classifier is checked against a spread of real commands (the catastrophic cases refused, the read-only cases quiet, `rm -rf /usr/lib` refused but `rm -rf /usr/local/myapp` merely destructive, `cd X && …` peeled so navigation cannot hide what follows, and the out-of-bounds paths refused from the shell as well as from `files`); the rendered prompt is checked to contain the action, the level, and the command rather than an internal scope glob; and the consent rules are checked end-to-end through the engine — a read-only command runs with no approval, a mutating one asks, **approving one shell command does not cover the next**, a destructive approval is never remembered, and a refused command is stopped *and* reported.

- **`SPEC.md`** — the requirements and design source of truth. Covers purpose, design principles, the harness/stack decision, the capability substrate, Generative UI, the model stack, memory, trust & safety, the cabinet, runtime, the Discord surface, phases, the P0 definition and build order, reconciliation notes, and open implementation details.
- **`ARCHITECTURE.md`** — the build-level companion: system context, component map, repository and package layout, runtime & process model, the five core flows, the data model, the interface/protocol contracts, the security architecture, failure modes, extension points, and the phase map.
- **`AGENTS.md`** — working rules for any agent operating in this repo: stack, build order, and the reach/restraint boundaries.
- **`CHANGELOG.md`** — this file.
- **Custom harness** — a standalone Python 3.14 + uv daemon, chosen over extending an existing agent framework (§SPEC 3).
- **The turn engine** — a single loop (Discord in → context → model → tools → output) that serves every surface and every cabinet seat, so seats are identity + charter + memory namespace rather than separate processes.
- **Capability substrate** — the tool-making machine:
  - **Layer 1 — Primitives:** `shell`, `code`, `files`, `http`, `browser`, `search`, `schedule`, `process`, `memory`, `discord`, `ask`, `secrets`.
  - **Layer 2 — Authored capabilities:** the model writes `run.py` + `manifest.toml` on demand; scheduled runs execute the artifact **without the model** (the model authors, the runtime disposes).
  - **Layer 3 — Resource registry:** declarative external access in three classes (local CLIs, remote APIs, devices), provisioned owner-driven.
  - **The invariant:** any request resolves to a new capability, a new resource, or — rarely — a new primitive. **Never a hand-coded feature.**
- **Capability lifecycle** — `born → verified → scheduled → monitored → re-authored | killed`; growth by proposal only, never autonomous authoring.
- **Melinoë verification gate** — every newly authored capability is run once and checked (output shape, side effects, scope compliance) before it may be scheduled.
- **Generative UI subsystem** — model-authored declarative **UI spec** + a deterministic renderer; a bounded, extensible component vocabulary; a custom theming engine mapping **mood + context → validated tokens**; loopback serving via Tailscale Serve; Taildrop push; owner-approved components and tokens.
- **Approval-by-rendering** — visual artifacts are approved *on the served artifact* (live preview, in-place tuning), while action approvals stay as Discord buttons.
- **Control surface** — the dashboard is interactive: tuners for presentation tokens and system knobs; tuned values enter a pending set and are adopted on approve.
- **Internal calendar & integrations** — a first-class internal calendar store (file-first `.ics` + SQLite index) that is **internal-authoritative** for a dedicated `Daedalus` calendar, with a bidirectional `gcal` resource (**read all, write own**) and a full **calendar dashboard view** (day / week / month, create / edit / delete). Plus **read-only mail**: Gmail `readonly` scope, read + summarize only, no send.
- **Setup wizard** — an interactive, **re-runnable** first-run wizard (`scripts/setup.sh`, `template.sh`-based): preflight · layout · secrets · owner identity · models · local runner · Discord · resources · trust posture · runtime · **hardening** · and a mandatory **`daedalus selftest`** that fails loudly on any red item. It is the owner-driven provisioning surface for secrets and access.
- **Memory model** — file-first (human-readable markdown + SQLite index) with provenance, confidence, supersession, and bitemporal validity; modeled facets; `local_only` never sent to remote models.
- **Trust & safety model** — trust tiers (self-contained ⇒ auto; anything leaving the machine ⇒ explicit approval), provenance labels, sink policy, quarantined reader, risk tiers, standing grants.
- **Cabinet (8 seats)** — Daedalus (coordinator), Athena (knowledge & coding mastery), Odysseus (research), Calliope (creative voice), Icarus (engineering & prototypes), Heracles (the body), dave (laptop ops), Melinoë (security & verification).
- **Typed durable seat protocol** — `request / ack / partial / result / verify / failure / cancel` with correlation/causation IDs; at-least-once with dedup.
- **Runtime & process model** — systemd `--user`, `enable-linger`, journald, a cron-pumped scheduler, capabilities as capped child processes, AC/battery power profiles, sleep→resume digest, lid-closed awake.
- **Discord surface** — one bot, DM-first, free-text, buttons for approvals, long tasks in threads.
- **Model stack** — NVIDIA NIM free tier behind one OpenAI-compatible `base_url` with fallback `stepfun-ai/step-5-preview → z-ai/glm-5.3-flash → Z.ai glm-4.7-flash → glm-4.5-flash`, fastest first, 60s stall ceiling each; Z.ai direct as a second free stack; **Bonsai 8B (1-bit)** local for offline fallback and cheap tasks.
- **Interface decisions** — output modes (`message` · `file` · `serve` · `push`); the MCP seam (consume external servers, expose the capability registry) as a forward path, not a P0 build.
- **Phases** — P0 (loop + substrate + proof-of-life) through P6 (group chat), with seats introduced one at a time.
- **P0 definition** — the unseen free-text request, completed end-to-end with nothing hardcoded; the substrate is in P0 because the proof-of-life cannot happen without it.

### Changed

- **A refused calendar call says what it heard, and hands back what the next call needs.** A model
  that reached for a plausible alias (`id` for `uid`) or sent only a date was told only what was
  *missing*, so it repeated the same wrong key — three identical failures in the owner's own turn.
  `op=update`/`op=delete` errors now echo the argument names the call actually carried (`You sent:
  `id`, `start``), and when a set matches nothing the reply carries the ids that *do* exist instead
  of sending the model round again on a separate `list`. *Supersedes:* the bare "needs `uid` or
  `match`" refusal.

- **Mail moves to the end of the plan.** Read-only Gmail was bundled with the calendar
  under "Calendar & integrations" (ARCHITECTURE §8.2). The owner cut it away: the calendar
  is the half that earns its keep, and a read-only mail scope that is never exercised is a
  standing risk taken for nothing. Mail now sits **after P6**, deliberately last (SPEC §10).
  The calendar half is built; `gcal` and the sync path it needs are not.

- **`browser` has a real backend.** Playwright is now a dependency and its headless
  Chromium is installed, so the primitive does the thing it advertises instead of
  returning *"browser backend not installed"*. Verified against a **local** page rather
  than the open network: JavaScript execution, selector narrowing, whole-page reads, and
  screenshots all round-trip. Chromium's footprint is paid only while a page is open.
  *Supersedes:* the headless-browser backend as an open detail (SPEC §13).

- **An approval prompt names the operation, and its buttons say what they do.** The
  headline came from the tool's static `title`, and `capability` is six tools in one —
  so a *delete* was announced as **"Approval needed — Author a capability"** while the
  body said "delete the authored capability". The first line the owner reads described
  the wrong action, which is exactly how the prompt stopped being trusted. A tool now
  supplies a per-call title (`Tool.title_for(args)`); `capability` returns
  *Delete a capability* / *Run a capability* / *Schedule a capability* / … The two
  buttons are likewise per-call (`Tool.approval_labels(args)`): **Delete it / Keep it**
  for a delete, **Do it / Don't** otherwise, instead of a generic Approve/Deny that
  forced a re-read to know what was being chosen. A denial's resolved line now also
  says **"nothing ran"**, so the outcome is stated rather than implied.
  *Supersedes:* the static `title` and the fixed Approve/Deny labels.

- **A capability always runs in its own venv.** `ensure_env` returned the host
  interpreter whenever a capability declared no `deps`, so a no-dependency artifact
  ran on the daemon's own environment — it could import the daemon's installed
  packages, and what it could reach stopped being described by its manifest. The
  first dependency a capability gained would also have resolved against a shared
  environment. Isolation is the point, not a side effect of having deps; `uv venv`
  is cheap enough to pay on every capability. *Supersedes:* the `not manifest.deps`
  early return.
- **The cheap local route carries the same 60 s stall ceiling as the chain.**
  `DEFAULT_CHEAP` set no `timeout_s`, so it silently inherited the dataclass default
  of 180 s — three times the ceiling every other entry is held to, and the rule that
  each provider bounds *silence* rather than answer length applied to exactly the
  provider most likely to hang (a local runner with no weights pulled, or mid-load).
  *Supersedes:* the un-set timeout on the local entry.
- **The CLI REPL no longer swallows an empty turn.** Every surface already sends
  through `deliver()`, which substitutes an explicit "no output" line for a turn with
  no text; the terminal loop alone still had `if result.text`, so the one surface a
  developer uses to reproduce a bug was also the one that went quiet on the exact
  failure it existed to show. It now uses `deliver()` like the rest. *Supersedes:*
  the inline `if result.text` guard.

- **Stale design-doc lines brought back in line with the code.** `SPEC.md` still read
  `Status: design settled; P0 not yet built`, contradicting the implemented loop the
  rest of the file already describes — it now reads P0-implemented, `0.1.0` pending
  the Discord proof-of-life. `SPEC.md` §11 step 3 listed 11 primitives and omitted
  `process` + the `capability` tool the substrate runs on; both are now named.
  `ARCHITECTURE.md` said "11 irreducible" tools, showed `surface/discord/dm.py +
  threads.py` (which do not exist — the surface is `bot.py + approvals.py` over
  `surface/base.py + cli.py`), omitted `tools/guard.py + capability.py` from the
  layout, and showed the timer calling `dispatch_tick()` directly where the daemon
  actually pumps its own ticks in-process (`main.py` `_ticker` → `dispatch_tick`).
  `pyproject.toml` still said "a run of 52 tests" with 112 collected. No behaviour
  changed; the docs describe the system that exists. *Supersedes:* the pre-build
  status line and the pre-implementation component counts.

- **A parameter that carries the substance of an action is now declared `required`.** An LLM treats a
  large *optional* string argument as skippable, and drops it without comment. That single behaviour
  was the whole of P0's blocker (see *Fixed*). The rule is now applied across the primitives: the
  schema's `required` list is the union of what its ops need, exactly as `process` already did with
  `name`. Where a payload belongs to only one op, the description says so and says what to pass for
  the others. `files.content` · `memory.content` and `memory.query` · `capability.run_py` ·
  `process.command` are all required now. *Supersedes:* the earlier convention in which the one
  parameter an op could not work without was the one parameter the schema let the model omit.
- **The charter tells the model not to tour the codebase.** One failed attempt spent its entire step
  budget listing, globbing, and reading its own implementation — including a single 25,058-character
  file read — and never reached the task. The charter now states that a tool's description *is* the
  contract, that reading its own source is unnecessary, and that a file is read only when the request
  is about that file, in the narrowest slice that answers the question. This is guidance about
  conduct, not a hardcoded flow.
- **The model chain now leads with the fastest entry.** Observed live: `z-ai/glm-5.3-flash` cold-starts past
  its timeout while `stepfun-ai/step-5-preview` answers in ~1.3s warm, so the primary was the *slower*
  of the two and every cold turn paid a stall before failing over. Order is now
  `stepfun-ai/step-5-preview → z-ai/glm-5.3-flash → Z.ai glm-4.7-flash → glm-4.5-flash`, and every entry
  carries an explicit **60s stall ceiling** (`timeout_s`). Because the client streams, that ceiling bounds
  *silence*, not answer length — a long answer that keeps producing bytes is never cut off. *Supersedes:*
  the GLM-first order chosen when the chain was first written, which was never measured.
- **Credentials are adopted into the encrypted store, and the wizard says so.** The wizard instructed the
  owner to drop the OAuth client JSON at `resources/creds/gcal-client.json` — plaintext at rest, and
  inconsistent with the resolution actually taken (the credential lives in the Fernet store as
  `secret:gcal-client`). The resources stage now asks for the downloaded file, **encrypts it into the store
  and deletes the plaintext copy**, and records `cred_ref = "secret:<name>"` in `registry.toml`. *Supersedes:*
  the `creds/<name>.json` reference and the "chmod 600 it" instruction, which asked the owner to protect a
  file the runtime never reads.
- **The wizard no longer carries a literal copy of the default config.** It emits `config.toml` from
  `daedalus.config.default_config_toml()` and, if that import fails, **stops and says so** instead of writing
  a fallback. That fallback had already gone stale twice in one evening — dead model IDs, then a corrected
  order it did not have — and a wizard that writes a config the runtime has moved past is worse than one that
  refuses. *Supersedes:* the handwritten heredoc fallback.
- **Inbound messages are logged; silent drops are over.** A dropped message and a dead daemon were
  indistinguishable from outside, which cost a real debugging session: the owner's DM produced no log line,
  no error, and no reply, because it never reached the bot at all. The surface now logs each inbound DM (by
  **length, never content**), logs *why* a message was dropped (not on the allowlist; not a DM — the surface
  is DM-first), and at startup **resolves the allowlist** and reports who it will serve, so a wrong owner id
  is loud instead of invisible. Strangers still get no reply.
- **A typing indicator is held for the duration of a turn.** A turn can take a minute or more — a cold model,
  tool calls, an approval round-trip — and the chat showed nothing until the answer arrived. The indicator is
  held for the *work* and released before the reply is sent, and a typing failure is swallowed: it is
  cosmetic and must never cost a turn.
- **Harness:** extending the prior Hermes-based runtime → a **custom standalone harness**. Hermes is divergent from the target and does not fit; the prior `daedalus.md` plan is retained as a deep architecture reference.
- **Models:** a Nemotron/OpenRouter-based stack → **NIM GLM chain + Z.ai + Bonsai 8B local**. (The prior provider's free-tier daily cap was the root cause of the earlier system's failure; the fix is largely a provider swap.)
- **Cabinet:** six seats → **eight**, adding **Athena** and **Heracles** and renaming **Orpheus → Calliope**; the original six had no owner for knowledge/coding mastery, the body, or a distinct creative voice.
- **Execution posture:** container-per-task → **host-native, kept light**, with the capability manifest as the leash; containers reserved for genuine third-party code.
- **Approvals:** approval-spam → **standing grants** for bounded, scoped, revocable capability.
- **Remoting:** public exposure → **Tailscale Serve only** (loopback-bound, **no public Funnel**).
- **Output taxonomy:** a single "output modes" list → **output channels** (`message` · `file` · `serve` · `push`) **+ external actions** (mail, calendar, git, social, payments, data). The first is delivery to the owner; the second is acting on other systems — they were wrongly merged.
- **Settings are home-relative:** the grant store and the rate-limit ledger resolve from `$DAEDALUS_HOME` rather than a process-global path, so a self-test or a second install can never read or write another install's state.
- **The approval prompt now says what is happening, in the owner's words.** It read
  `**Approval needed** — shell on shell:*`, then a reason, then the command. The owner's response
  was, verbatim: *"I genuinely have no idea what shell on shell:* is about, or what that command is
  supposed to do, there's no explanation."* They were right: the prompt named an internal tool and a
  scope glob, and omitted the one thing that decides the answer — what the action actually does. It
  now reads: a human title (`**Approval needed — Run a shell command**`), **What it does** (`delete
  files`), **Risk** (`destructive — this cannot be undone`), the reasons that raised the level, then
  the exact command in a code block, and finally why consent is being asked for at all. The tool's
  raw arguments are summarised rather than dumped, with oversized values replaced by their length
  (`run_py: …(1300 characters)`) so a 1,300-character program cannot bury the prompt.
  *Supersedes:* the `action` + `scope` head line and the bare joined-arguments body.
- **The reason a *local* action asks now explains itself.** The previous session replaced the
  universally-wrong *"egress requires approval"* with *"`<tool>` always asks before it runs"* — true,
  but a description of the mechanism rather than a reason, and it still sounded like jargon to the
  owner. It now reads *"it can change things on this machine, so it asks first"*; egress keeps its
  own wording, which now also says that approving remembers the host. *Supersedes:* the
  *"always asks before it runs"* wording added earlier in this same unreleased section.
- **`shell` risk is derived from the command, not declared once for the tool.** It was a flat
  `ask`, which meant `git status --short` and `rm -rf ~` produced the same prompt. Asking is the
  expensive part of the relationship, so it has to be reserved for what it is for: a read-only
  command is self-contained and now runs without a prompt (matching SPEC §6's rule that
  self-contained actions are auto), anything that can change state asks every time, and a
  `catastrophic` command is refused rather than offered. The consequence is that a prompt now
  *carries information* — if everything asks, "it asked" stops meaning anything.
  *Supersedes:* the flat `risk = ask` on the shell primitive.
- **Consent is narrow: an approval covers what was approved, and nothing more.** Approving any shell
  command wrote a standing grant scoped `shell:*` — there is no scope that describes a command, so
  the fallback glob covered every command that would ever run afterwards, including destructive ones
  the classifier had merely rated *ask*. Three rules now hold. A tool is **grantable** only when its
  scope actually describes the call, and `shell` is not. A multi-operation tool is scoped **by
  operation** (`capability:author` no longer covers `capability:remove`). And a **destructive**
  approval is never remembered at all — *consent for something irreversible is not consent for it
  later, forever.* *Supersedes:* the `{tool}:*` fallback grant and the unconditional `grant_from` on
  approval.
- **`SPEC.md` §6 and `ARCHITECTURE.md` §9.1 / §10** now carry the rule that decides a call's tier
  deterministically, the level → risk-tier mapping, the narrow-consent rules, and the environment
  scrub, so the design documents describe the system that exists rather than the one that was
  intended. §6's *"strong confirmation retained for … irreversible ops"* is the line this change
  finally makes true.
- **The suite is 112 tests** (from 65) — the new 47 in `tests/test_classify.py`, and no existing test
  was weakened to accommodate the change.

### Fixed

- **A series is several entries in the file, and a change now moves all of them.** One repeating
  event is a rule, an `EXDATE` per skipped day, an `RDATE` per added day, and a `VEVENT` of its own
  per instance the owner edited — each with its own `DTSTART`, joined by the series' `UID` and pinned
  by `RECURRENCE-ID`. `shift` rewrote the rule's `DTSTART`/`DTEND` and nothing else, so shifting a
  series that had exceptions made the calendar wrong **twice at once, in opposite directions**: the
  skipped day came back (its `EXDATE` named a date the rule no longer produced, and it looked like any
  other event) and the edited instance was stranded where it was (which reads as one more thing on).
  Nothing errored; the week just quietly held events that were not there. The delta is now applied to
  the rule *and* to everything it excepts, by the same delta — an instance moved to the 13th is *the
  12th's* instance, so a series shifted a week back puts it on the 6th — and `UNTIL` moves with the
  rule it bounds, since `UNTIL` is a date inside it. `COUNT` needs nothing: it counts from a `DTSTART`
  that is moving too. An explicit new `start` is a translation as well and carries the same exceptions.
  Fifteen tests, including the round trip: shift a series a week back and forward and the file reads
  as it started. One consequence is a change in its own right — an explicit new `start` now carries
  the event's **length** with it (the end moves by the same delta), where before the end stayed put
  and a moved half-hour standup silently became one that ended before it began. A translation is a
  translation, and it cannot be one rule for the series and another for the instances.

- **A `scope="future"` split hands its overrides to the series that inherits them.** `_update_future`
  truncated the master's rule at the cut and gave the new tail a fresh `UID` — but left the instances
  the owner had edited pointing at the *master*, whose rule now stopped before them. Nothing superseded
  the tail's own occurrence on that day, so one moved standup became **two**: the tail produced the
  day the owner had moved away from, and the override showed up at its new date beside it. Found while
  making the shift above carry its exceptions — the same question, one layer over. The inherited
  `VEVENT`s are re-pointed at the tail's `UID`, which is what makes an override that series' override.

- **The page policy now travels as a header, not only inside the document.** The renderer writes the
  CSP as a `<meta>`, and it was the only place it existed — but a `<meta>` policy begins to apply
  where it is parsed, which leaves every byte before it, doctype and head included, served with no
  policy in force at all. The loopback server now sends `Content-Security-Policy` on an HTML
  response, and only on HTML: a policy attached to a PNG is not a policy, it is noise. Found while
  wiring the dashboard through the real socket, which is the part of T7 a unit test of `get()`
  cannot cover.

- **A cross-prop rule no longer runs on a key.** `Component.check` refuses a `table` row wider than
  its headings and a `calendar` day that is not `[date, weekday, what is on it]` — but it ran during
  parsing, and a prop that is still a **binding** is a key, not a shape: the calendar view reached
  into `props["days"]` and iterated a `Binding`, which is a `TypeError` rather than a refusal. The
  rule now waits for the pass that has the values, and a test pins that it is *deferred, not skipped*
  — a bound row of the wrong width is refused at bind time, in the same voice as a literal one. Found
  the moment the new dashboard check asked a bound calendar to render, which is what a gate is for.

- **The proactive gate no longer goes red at lunchtime.** The selftest probe drives a **pinned**
  sequence — a silent 03:00 tick, then a noon tick that must speak — but each run recorded the job's
  `last_run` from the **wall clock** while judging dueness against the pinned time. So the probe passed
  all morning and, from 12:01 onward, failed: the pinned noon tick had become *"already run"*, and
  `daedalus selftest` reported 10/11 for the rest of the day. The gate was wrong, not the thing it
  gates — and a health check that is only honest before lunch is one the owner learns to ignore. A
  tick given a time is now governed by that time. Two tests pin times in the **past** on purpose, so
  an implementation that reads the wall clock fails whenever the suite happens to run rather than only
  after noon. Found by the clock crossing noon during this work; it had been wrong since the probe was
  written, because no test had ever ticked the same job twice at pinned hours.

- **The repeat guard no longer collapses two different calls that fail the same way.** It keyed the
  failure signature on the tool name and the *error text*, so three genuinely different attempts —
  authoring a capability under three different names, each failing because `run_py` was missing —
  looked like one repeated step and the turn was cut short after the second. The signature now
  includes the call's arguments (reusing `_call_signature`), so only a genuinely identical call trips
  `MAX_IDENTICAL_FAILURES`; and the dead-end message shows the arguments it kept sending, so the
  owner is asked to diagnose from the whole call rather than half of it. *Supersedes:* keying the
  guard on `name: detail`.

- **A schema bump could have shown the owner an empty calendar.** Changing the index shape above
  exposed a fault in how the cache was replaced. `events.ics` is the truth and `index.sqlite` a
  build over it, so a shape change **drops and rebuilds** rather than migrating — but the record of
  *which* `events.ics` revision the cache was built from survived the drop. An emptied cache that
  still matches the file it came from looks current, so the store declined to rebuild and answered
  "no events" — for every existing install, on the next restart, until the `.ics` happened to
  change. Emptying the cache now clears the revision it claimed, which is what "rebuild me" means.
  Found by asking what the version bump would do to a calendar that already has events in it,
  rather than to a fresh one in a test directory. *Supersedes:* carrying `source_revision` across a
  schema change.

- **`RDATE` was read and then thrown away, taking the occurrences with it.** An event's extra
  dates — "every Monday, *and also* Friday the 16th" — were listed as a property the reader knew,
  parsed into the field map, and then never put on the event and never written back. The result was
  the worst shape a bug can take here: silent, and destructive on *write*. Import a file with an
  `RDATE`, then let any tool call rewrite `events.ics`, and the added dates were gone — the
  occurrences simply stopped happening, and nothing said so. `RDATE` is now a first-class part of
  the event:
  - **The recurrence set is what the RFC says it is.** `RRULE ∪ RDATE − EXDATE`. An added date is
    a real occurrence on every read (`agenda`, `busy`, `search`, free/busy), and leaving one out is
    the same act whether a rule produced it or a person typed it — so `scope=this` on an added date
    works, and writes the `EXDATE` that means it.
  - **A rule is not required.** An event with no `RRULE` and three `RDATE`s is a legal way to say
    "this, and also those" — it produced only its own start before, and now produces all four.
  - **An added date on a day the rule already has is not two standups.** Deduped by occurrence
    start, so a hand-written `RDATE` that repeats a rule date stays one meeting.
  - **`scope=future` splits them too,** on the same date boundary as the rule and the exclusions —
    a split series that kept both halves' added dates would double-book the owner.
  - It also round-trips: `RDATE` is written back in the shape it was read, `VALUE=DATE` for an
    all-day event included, and the definition view (`list`, `get`) says `+2 extra dates` so a view
    of the *definitions* does not contradict a view of the occurrences. The table shape changed with
    it, so `SCHEMA_VERSION` goes to 4 and the derived cache **rebuilds** — as it always does,
    `events.ics` remaining the truth.
  - Not a tool parameter: the model has no `rdate` argument, and does not need one (two events say
    the same thing). This is a *fidelity* fix — the store must not lose what the owner's file says.

- **The `browser` primitive crashed on its own default argument.** `op=text` with no
  selector called `Page.inner_text()`, which *requires* a selector, and raised
  `TypeError` — so the tool threw on its most common use, "read this page". `op=html`
  had the same fault. Both now read the whole document when no selector is given
  (`inner_text("body")` / `content()`) and narrow to the element when one is. Found by
  running the tool rather than reading it. *Supersedes:* passing a `Page` to a method
  that expects a `Locator`.
- **A malformed call was described as destroying "an unnamed capability".** The model
  sent `capability op=remove` with no `name`; `assess` filled the gap with the literal
  string *"an unnamed capability"*, so the owner was shown
  *"delete the authored capability 'an unnamed capability' — destructive, this cannot be
  undone"* for a call that would in fact delete nothing (`remove("")` finds no
  capability). The prompt now says plainly that the call named no target, and its risk
  line reads *"incomplete call — approving it would change nothing"* rather than the
  irreversible-delete warning. The tier stays `destructive` on purpose, so approving a
  malformed call can never mint a standing grant for future deletes.
  *Supersedes:* the `or "an unnamed capability"` fallback.
- **Denying a call no longer invites the same prompt straight back.** A denial returned
  `ok=False` and was counted by the repeat-failure guard, which only stops after three
  identical failures — so one broken call produced the *same* approval prompt twice and
  the owner clicked deny twice, unsure whether the first click had registered. A denied
  call is now recorded by name + arguments, and the engine stops the turn the moment the
  model re-issues it, saying so. *Supersedes:* treating a denial as an ordinary failure
  that the model may simply retry.

- **Scheduled alerts were titled with an internal job handle.** A capability's notification is
  rendered as `**<title>**` plus its body, and the title fell back to `job.name` — the internal handle
  `cap-disk_free_check` — rather than the capability's own name. The owner would have read a private
  identifier every weekday morning. It now falls back to `job.target`, the name the owner and the
  model actually use. The authoring contract also now states the shape a notification should take
  (`{"title": …, "text": …}`) and that an empty list means stay quiet, so a capability can title
  itself. *Supersedes:* the `job.name` fallback.
- **The acceptance request failed on a tool schema, not on the model.** The owner asked, in free
  text, for a weekday disk-space check. The turn reached the capability substrate, asked for approval,
  was approved — and then spent its **entire sixteen-step budget** calling
  `capability op=author -> author needs name and run_py` and produced **nothing**: no capability, no
  job, no memory, and not one log line. The model was sending
  `{"op": "author", "name": "disk_free_check", "schedule": "41 7 * * 1-5", …}` — every field except
  `run_py`, the program itself. It was not one bad model, one bad prompt, or one bad phrase: with
  `run_py` optional, `stepfun-ai/step-5-preview` left it out under a minimal system prompt, under the
  full charter, with one tool and with thirteen. Add `run_py` to the schema's `required` list and the
  same model, same prompt, same request, returns 1,313 characters of correct Python. The fix is the
  schema change recorded under *Changed*; the tool's failure message now also names the missing
  argument and says to retry with the source included. Verified end-to-end: the capability
  `disk_free_check` was authored, run once by the Melinoë gate, registered, and scheduled at
  `41 7 * * 1-5`, and its alert branch fires correctly when the threshold is crossed.
  *Supersedes:* the assumption that the authoring interface was fine and the model was at fault.
- **An empty model turn was delivered as silence.** When the model answered with neither text nor tool
  calls, the engine returned an empty string and the caller sent nothing at all — the owner got no
  reply and no error, which is indistinguishable from a dead daemon. A turn now always says something:
  if the model returns nothing, the owner is told so explicitly. *Supersedes:* the `if result.text`
  guard, which made an empty answer look like no answer.
- **The "cheap" route could hard-fail a trivial turn.** `_chain("cheap")` returned only the local
  provider. Ollama is running with **zero models pulled**, so any turn the router classified as cheap
  — a short message, or one containing a marker like *"is this"* or *"summarize in one line"* — would
  have failed outright with no attempt at a working provider. The cheap route now returns the local
  provider followed by the full chain, so a missing local model is a slower answer rather than a dead
  turn.
- **Approval prompts gave the wrong reason for non-egress tools.** The middleware fell through to
  *"egress requires approval"* for every ask-tier tool, including ones with no network scope at all.
  The owner approved a `capability` call reading *"egress requires approval"* — a sentence that was
  simply false about what was happening, and that framed a local file write as data leaving the
  machine. A non-egress ask-tier tool now says *"`<tool>` always asks before it runs"*, and egress
  keeps its own wording. *Supersedes:* the single hardcoded reason.
- **A repeated identical failure now stops the turn.** Sixteen steps on one broken call was not a
  model quirk to tolerate; it was the loop failing to notice that a retry was not progress. Three
  *consecutive* identical failures now end the turn with a plain explanation of what kept failing and
  what the owner might say instead. Progress in between resets the count, so a genuine retry with a
  different approach is unaffected.
- **One oversized tool result could poison the rest of the turn.** A single `files` read returned
  25,058 characters straight back into the conversation, and every model call after it degraded. No
  single tool result may now add more than 8,000 characters; a truncated result says so and tells the
  model to ask for a narrower slice instead of the whole thing.
- **`files op=write` could create an empty file and report success.** With `content` optional, the
  model could omit the bytes, and the tool would write an empty file and return `ok` — a silent data
  loss wearing a success message. `content` is required, and the schema-audit test keeps it that way.
- **`memory op=recall` with no query reported an empty memory.** An omitted `query` searched for the
  empty string, which matches nothing, so the owner would be told *"(nothing found)"* when the memory
  was full. `query` is required, and its description states what an empty query does.

- **The daemon deadlocked on startup and never connected to Discord.** `cmd_run` awaited
  `surface.wait_ready()` *before* calling `surface.start()`. A surface signals readiness from inside
  its own connection loop, so the daemon waited for a connection it had not yet begun: the process
  came up, opened **no socket**, dialed nothing, logged nothing, and systemd reported it
  `active (running)` for 32 minutes. The Discord surface had therefore never once worked — no user
  could have reached the agent by DM. The lifecycle is now extracted to `serve()`, which launches
  `start()` as a task and *then* waits for readiness, so an early `start()` failure is raised loudly
  instead of hanging the unit forever. Caught by a direct check of the live process (zero sockets
  owned by the daemon pid), not by any test that existed at the time.
  *Supersedes:* nothing — this was never working.
- **The self-test reported green on a daemon that never came up.** This is the more serious of the
  two: the gate was 8/8 while the bot was silent, because every check verified *configuration* and
  none verified that the process actually starts. A gate that cannot see the primary failure mode is
  decoration. There is now a **`daemon`** check that drives `serve()` with a surface whose readiness
  is reachable only through `start()` — the exact ordering that was broken — and asserts it comes up,
  pumps ticks, and shuts down. `tests/test_daemon.py` adds three tests that fail on the old ordering
  and pass on the new one; the regression was verified to bite by re-running the old ordering, which
  hangs with the surface never started.

- **The default provider chain named models that do not exist.** `DEFAULT_HEAVY` shipped the
  shorthand `glm-5-3 → glm-4.7 → minimax → kimi`, and the first live probe failed exactly as it
  should: `HTTP 404: 404 page not found` from NIM. Interrogating the catalogue directly showed that
  `glm-4.7` and `minimax` are not in it at all, `moonshotai/kimi-k2.6` is *listed* but answers 404 on
  completion, and `z-ai/glm-5.3` does not answer inside a useful timeout. The chain is now
  **`stepfun-ai/step-5-preview` → `z-ai/glm-5.3-flash` → Z.ai `glm-4.7-flash` → Z.ai `glm-4.5-flash`**,
  with the first two confirmed to return text *and* well-formed tool calls, and the Z.ai pair
  confirmed to exist (the spec's `glm-4.7-flash` is real — it simply lives on Z.ai, not NIM).
  The wizard's emergency literal template was carrying the same dead IDs and is corrected with it.
  *Why this consumed a live probe:* the catalogue is the only source of truth for model IDs, and no
  amount of local reasoning substitutes for asking the provider. *Supersedes:* the shorthand in
  `SPEC.md` §4, now annotated in place rather than quietly overwritten.
- **`selftest` mutates the install it is verifying.** The layout and secret checks wrote into the
  real `~/.daedalus`, so running the acceptance gate twice left behind a fake `daily_focus`
  capability, a scheduled job, and memory rows. A verification gate that dirties what it verifies
  cannot be trusted twice. Layout and permission assertions now read the real home **read-only**;
  every mutating check runs against a throwaway `tempfile.mkdtemp` scratch home that is removed
  afterwards. Two consecutive runs now leave **no trace**, and that property is itself a regression
  test (`tests/test_selftest.py`).
- **The live probe only ever tested the first provider**, so a fallback could rot unnoticed. It now
  walks the chain in order, stops at the first provider that answers, applies a shortened
  per-provider timeout so a cold start cannot stall the gate, and — on a 404 — says outright that
  the model ID is not in the provider's catalogue.
- **`Scheduler.add_job` crashed on any cron expression** — the `cron` parameter shadowed the `cron` module inside the method, so validation hit a string. The validator is now imported under an explicit name. Caught by the self-test, not by luck.
- **`Manifest.save()` assumed its capability directory existed** and raised `FileNotFoundError` when writing a manifest into a fresh directory; it now creates the directory first.
- **A malformed line in the CLI REPL** (a stray walrus expression) would not have run; rewritten to a plain event construction.
- **Approving one shell command silently consented to every later one.** The owner approved a
  `capability` call, then watched two `shell` prompts arrive — `git status --short && …` and a
  `systemctl …` query. Consenting to either wrote a standing grant scoped `shell:*`, because
  `derive_scope` falls back to `{tool}:*` when the arguments carry no URL or path, and a shell
  command's arguments are just its command text. From that moment **every** shell command ran with no
  prompt at all — including the destructive ones the new classifier rates merely *ask*: `rm -rf
  ~/code`, `git reset --hard`, `git push --force`. Only the `catastrophic` tier would still have been
  refused, which means the owner's grant was doing more of the guarding than the classifier was. It
  was found by asking the classifier's own question back at the grants — *what can this approval
  reach?* — and not from a failing test, because no test had ever approved a command and then run a
  different one. Three rules now close it, recorded under *Changed*.
- **A tool that could not describe itself was labelled "read-only".** The default `assess` returned
  `Level.OK`, so `code` and `discord` rendered as `**Risk:** read-only` — a false statement in the
  one place the owner is being asked to trust a judgement. A prompt is only worth reading if its
  labels are true, so the default now follows the tool's own risk tier: a tool that has not said what
  it does is *not known* to be read-only, and an ask-tier call is labelled as one. Caught by printing
  the rendered prompts and reading them, which is also how the next item was caught.
- **A refused command was stopped silently.** When the classifier blocked a call, the model was told
  and the owner was told nothing — so an agent that tried `rm -rf /` and was stopped was
  indistinguishable from an agent that never tried. The owner now receives a short note naming what
  the agent wanted to do and confirming nothing ran. Found by reading the blocked path rather than
  the allowed one.

### Removed

- **Hermes as a dependency** (runtime decision reversed).
- **Nemotron models.**
- **Heavy local models** — they do not fit the available RAM, and the latency is unacceptable.
- **Fixed / keyword-routed behaviours** — no component ships a flow the model cannot compose around.
- **Public Funnel.**
- **Containers for first-party code.**

### Security

- **`local_only` memory could reach a remote model.** `build_system_prompt` injected
  the full recent-memory list into every turn's system prompt, with no regard for the
  data class — so a fact marked `local_only` rode out to NIM or Z.ai on the very next
  message, and a `memory op=recall` result did the same, straight back through the
  tool channel. SPEC §5 says `local_only` content never goes to a remote model; the
  rule was written but never wired. The engine now resolves the model chain for the
  turn and sets a single flag, `Session.local_only_ok`, that is true only when
  **every** provider the turn could reach is local — one remote fallback is enough to
  withhold the fact. The prompt assembler and the `memory` tool both read that flag,
  and the default is *withhold*, so a caller that never declared a local route cannot
  leak. *Supersedes:* the unconditional `memory.inventory()` in the system prompt.

- **Reach narrow** — local work reads the minimum needed; the agent never sweeps the filesystem, the vault, or unrelated projects to assemble context.
- **Personal data is out of bounds** — passwords, `.env`, private folders, and health / financial / relationship / location data are never opened, read, logged, summarized, or transmitted.
- **Bytes stay home** — nothing leaves the machine without approval; no secret or personal value in any prompt, request body, log, or message. **Structure travels; values stay local.**
- **Sink policy** — untrusted text may inform a draft but can never authorize a recipient, URL, command, or scope.
- **Approval binding** — approvals are bound to action hash + scope + expiry + idempotency.
- **The process environment is scrubbed** — `security/env.py`. Found while building the classifier, which is the first component to ask "what could this command actually reach?" Commands the agent runs inherit its environment, and that is a reasonable place for a credential to be sitting: the install's own `.env`, or the owner's shell when the agent is driven from the CLI. Whatever a command prints comes straight back into the conversation and on to a model provider. Every subprocess the agent launches — `shell`, `code`, `process`, and a capability's `run.py` — now receives an environment with the install's `.env` keys and anything whose name looks like a credential (`*_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`, …) removed, verified by a test that exports a key, runs `printenv` through the loop, and asserts the value never reaches the fed-back tool result. *Supersedes:* nothing — this was not previously considered, and the same rule ("structure travels, values stay local") simply had not been applied to the process environment, only to request bodies.
- **`config.py` claimed `.env` was loaded into `os.environ`; it never was.** `load_env` parses `$DAEDALUS_HOME/.env` into a dict and reads it from there. The docstring said otherwise, and that inaccuracy is security-relevant — reasoning about what a subprocess can inherit from a wrong statement about the environment is how a scrub gets skipped. The docstring now says what the code does.
- **`Approval needed — shell on shell:*` was shown for a command that would read a private key.** `files` refuses the out-of-bounds paths (`.env`, `~/.ssh`, `~/.aws`, `docs/private`, the vault) but `shell` could walk straight through that refusal, and reading a key into the conversation is precisely a path by which a value leaves the machine. The classifier now refuses those paths from *any* tool, which is the point: a guard on one tool is worth nothing.
- **A refused command is now reported to the owner.** Blocking was silent — the model was told the call was blocked and the owner was told nothing, so an agent that tried `rm -rf /` and was stopped looked exactly like an agent that never tried. A refusal now sends the owner a short note saying what it wanted to do and that nothing ran. Silence and safety should not be indistinguishable.
- **One approval used to cover every future command.** See *Fixed* — an approval for `shell` wrote a `shell:*` standing grant, so consenting to `git status` silently consented to `rm -rf ~/code` for as long as the grant lived.
- **Capability safety** — manifest-scoped access, resource caps, audit log, and kill switch per capability.

### Notes

- **Documentation set:** `SPEC.md` (what) · `ARCHITECTURE.md` (how) · `AGENTS.md` (working rules) · `CHANGELOG.md` (this file).
- **Open implementation details** (not architectural decisions) are tracked in `SPEC.md` §13: the local runner for Bonsai 1-bit weights, scheduler transport, the `manifest.toml` schema, the resource-provisioning flow, the headless-browser choice, the GenUI spec schema, the mood→token mapping, the dashboard regeneration trigger, the tuner set, long-running-process supervision, and capability resource-cap enforcement (`cpu` / `mem_mb` are declared but not yet applied to the child). The memory-recall embedding choice is **resolved** — FTS5, with embeddings kept as a seam.
- **gcal is provisioned but not yet implemented.** The owner's OAuth desktop client has been adopted into the encrypted credential store as `secret:gcal-client` (so it no longer sits as a world-readable file on disk), and `resources/registry.toml` carries a live `gcal` resource entry with `calendar.readonly` + `calendar.events`. **No code consumes it yet** — `integrations/gcal.py` and the internal calendar store of §5.5 do not exist, so today the agent can neither read nor write any calendar. The resource is registered carried-forward work, not a working integration.

<!--
Maintenance notes:
- Newest changes at the top of each list.
- Keep `Unreleased` accurate; never edit a released section — cut a new version instead.
- Link version headings to comparison URLs once a remote exists.
- Every entry answers: what changed, why, and what it supersedes.
-->
