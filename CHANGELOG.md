# Changelog

All notable changes to **Daedalus** are documented in this file.

- **Format:** [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
- **Versioning:** [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
- **Maintenance policy:** every user-visible change gets an entry under `Unreleased` as it lands. On release, `Unreleased` moves under a dated version heading. Entries are **verbose by policy** — record *what* changed, *why*, and *what it supersedes*, so the file is a decision record, not just a diff of names.

Change types: `Added` · `Changed` · `Deprecated` · `Removed` · `Fixed` · `Security`.

---

## [Unreleased]

_Design frozen; **P0 is implemented as code** — the loop, the primitives, the capability substrate, memory v0, the scheduler, the Melinoë gate, the Discord surface, and the setup wizard — verified by `daedalus selftest` (8/8 offline, 9/9 with `--live`) and a 44-test suite. `0.1.0` is cut once the owner runs the live proof-of-life end-to-end with real credentials._

### Added

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
  - **Tests** — 44 tests across cron, approvals, capabilities, memory, the engine loop, the files guard, the secret store, the daemon lifecycle, and the self-test's own isolation guarantee.

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
- **Model stack** — NVIDIA NIM free tier behind one OpenAI-compatible `base_url` with fallback `z-ai/glm-5.3-flash → stepfun-ai/step-5-preview → Z.ai glm-4.7-flash → glm-4.5-flash`; Z.ai direct as a second free stack; **Bonsai 8B (1-bit)** local for offline fallback and cheap tasks.
- **Interface decisions** — output modes (`message` · `file` · `serve` · `push`); the MCP seam (consume external servers, expose the capability registry) as a forward path, not a P0 build.
- **Phases** — P0 (loop + substrate + proof-of-life) through P6 (group chat), with seats introduced one at a time.
- **P0 definition** — the unseen free-text request, completed end-to-end with nothing hardcoded; the substrate is in P0 because the proof-of-life cannot happen without it.

### Changed

- **Harness:** extending the prior Hermes-based runtime → a **custom standalone harness**. Hermes is divergent from the target and does not fit; the prior `daedalus.md` plan is retained as a deep architecture reference.
- **Models:** a Nemotron/OpenRouter-based stack → **NIM GLM chain + Z.ai + Bonsai 8B local**. (The prior provider's free-tier daily cap was the root cause of the earlier system's failure; the fix is largely a provider swap.)
- **Cabinet:** six seats → **eight**, adding **Athena** and **Heracles** and renaming **Orpheus → Calliope**; the original six had no owner for knowledge/coding mastery, the body, or a distinct creative voice.
- **Execution posture:** container-per-task → **host-native, kept light**, with the capability manifest as the leash; containers reserved for genuine third-party code.
- **Approvals:** approval-spam → **standing grants** for bounded, scoped, revocable capability.
- **Remoting:** public exposure → **Tailscale Serve only** (loopback-bound, **no public Funnel**).
- **Output taxonomy:** a single "output modes" list → **output channels** (`message` · `file` · `serve` · `push`) **+ external actions** (mail, calendar, git, social, payments, data). The first is delivery to the owner; the second is acting on other systems — they were wrongly merged.
- **Settings are home-relative:** the grant store and the rate-limit ledger resolve from `$DAEDALUS_HOME` rather than a process-global path, so a self-test or a second install can never read or write another install's state.

### Fixed

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
  **`z-ai/glm-5.3-flash` → `stepfun-ai/step-5-preview` → Z.ai `glm-4.7-flash` → Z.ai `glm-4.5-flash`**,
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

### Removed

- **Hermes as a dependency** (runtime decision reversed).
- **Nemotron models.**
- **Heavy local models** — they do not fit the available RAM, and the latency is unacceptable.
- **Fixed / keyword-routed behaviours** — no component ships a flow the model cannot compose around.
- **Public Funnel.**
- **Containers for first-party code.**

### Security

- **Reach narrow** — local work reads the minimum needed; the agent never sweeps the filesystem, the vault, or unrelated projects to assemble context.
- **Personal data is out of bounds** — passwords, `.env`, private folders, and health / financial / relationship / location data are never opened, read, logged, summarized, or transmitted.
- **Bytes stay home** — nothing leaves the machine without approval; no secret or personal value in any prompt, request body, log, or message. **Structure travels; values stay local.**
- **Sink policy** — untrusted text may inform a draft but can never authorize a recipient, URL, command, or scope.
- **Approval binding** — approvals are bound to action hash + scope + expiry + idempotency.
- **Capability safety** — manifest-scoped access, resource caps, audit log, and kill switch per capability.

### Notes

- **Documentation set:** `SPEC.md` (what) · `ARCHITECTURE.md` (how) · `AGENTS.md` (working rules) · `CHANGELOG.md` (this file).
- **Open implementation details** (not architectural decisions) are tracked in `SPEC.md` §13: the local runner for Bonsai 1-bit weights, memory-recall embedding choice, scheduler transport, the `manifest.toml` schema, the resource-provisioning flow, the headless-browser choice, the GenUI spec schema, the mood→token mapping, the dashboard regeneration trigger, the tuner set, and long-running-process supervision.
- **gcal is provisioned but not yet implemented.** The owner's OAuth desktop client has been adopted into the encrypted credential store as `secret:gcal-client` (so it no longer sits as a world-readable file on disk), and `resources/registry.toml` carries a live `gcal` resource entry with `calendar.readonly` + `calendar.events`. **No code consumes it yet** — `integrations/gcal.py` and the internal calendar store of §5.5 do not exist, so today the agent can neither read nor write any calendar. The resource is registered carried-forward work, not a working integration.

<!--
Maintenance notes:
- Newest changes at the top of each list.
- Keep `Unreleased` accurate; never edit a released section — cut a new version instead.
- Link version headings to comparison URLs once a remote exists.
- Every entry answers: what changed, why, and what it supersedes.
-->
