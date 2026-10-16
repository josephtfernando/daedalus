# AGENTS.md — Daedalus

Daedalus is a single-user, local-first chief-of-staff agent that runs on this laptop and is reached over Discord. **`SPEC.md` is the design's source of truth** — build against it; if code and spec disagree, the spec wins until the owner says otherwise.

## Working here

- **Stack:** Python 3.14 + uv. Runtime and process model: `SPEC.md` §3, §8.
- **Build order:** P0, P1 and P2 are delivered — the loop + primitives + capability substrate, memory depth, and the proactive scheduler (quiet hours, a daily cap, and a wake that composes fresh). The internal calendar landed alongside them; `gcal` is provisioned but unbuilt. **P3 (seats, one at a time) is next.** No cabinet seat until the free-text proof-of-life holds from Discord (`SPEC.md` §11).
- **Start from the branch, not the whole tree:** read the `SPEC.md` section for the feature you're touching before writing code.
- **Tests live beside behaviour.** P0's acceptance test is the unseen free-text request (`SPEC.md` §11).
- **Maintain `CHANGELOG.md`.** Every user-visible change gets a verbose entry under `Unreleased` — what changed, why, and what it supersedes. On release, move `Unreleased` under a dated version heading.

## Reach and restraint

**Reach narrow.** Start from the exact thing named — a path, a symbol, a file — and widen only when the narrow reach comes back empty, then stop. The working set is this repo and `SPEC.md`; never sweep the home directory, the vault, or unrelated projects to assemble context — ask the owner instead.

**Personal data is out of bounds.** Passwords, `.env`, `docs/private`, the Obsidian `private/` folder, and health / financial / relationship / location data are never opened, read, logged, summarized, or transmitted — by the coding agent here or by Daedalus at runtime (`SPEC.md` §6). If a task appears to need any of it, stop and ask.

**Bytes stay home.** Nothing leaves the machine without the owner's explicit approval: no secret or personal value in a prompt, request body, log, or message. Outbound actions are proposals, never autonomous moves. The pattern throughout: **structure travels, values stay local** (`SPEC.md` §3.6, §6).
