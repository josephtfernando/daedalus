"""Watches — event-driven wakes, for the things a clock cannot know.

The scheduler knows *when*. It could not know *that*: "tell me when the build
finishes", "when the export lands in Downloads", "when that file disappears".
`watch` is the second kind of trigger, and it is deliberately not a second
scheduler: the same tick that already runs every minute stats each watched path
and fires through the same wake handler, so there is one clock, one record, and
one place a job was missed.

Why polling and not inotify: a single owner, a handful of paths, and a stat is
microseconds. `inotify` would add a dependency, a thread, and a watch-descriptor
limit for a feature whose resolution is *a minute anyway* — the tick that would
deliver the message. The trade is stated here so it is not rediscovered later.

Two bounds keep an owner-authored watch from becoming a drip:

* **`cooldown_s`** — a file being written continuously (a log, a build) fires at
  most once per cooldown, not once per tick.
* **`once`** — the default. A watch is usually "tell me when this happens", not
  "tell me every time". It is removed after it speaks.

A watch is the **owner's** instruction, like a reminder, so the proactive gate
does not silence it: the gate bounds the agent's choice to speak, not the
owner's. The cooldown and `once` are what keep that from meaning "always".
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("daedalus.scheduler")

#: How many watches may exist at once. The tick stats every one of them, so this
#: is the bound that keeps "a useful trigger" from becoming a second scheduler.
MAX_WATCHES = 20
#: The shortest cooldown a watch may declare. Below this, a file that changes
#: constantly turns the agent into a metronome.
MIN_COOLDOWN_S = 30.0


def signature(path: Path) -> list[int] | None:
    """What "unchanged" means for a path: its mtime and size, or ``None``.

    ``None`` is not "unknown" — it is *absent*. That is the fact a `missing`
    watch is built on, so it is carried, not swallowed.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


@dataclass(slots=True)
class Watch:
    id: str
    path: str
    note: str
    #: ``change`` — appeared or was modified; ``missing`` — disappeared.
    on: str = "change"
    once: bool = True
    cooldown_s: float = 600.0
    created_at: float = field(default_factory=time.time)
    #: The last signature the owner was told about. The baseline is taken at
    #: creation, so adding a watch never fires on what was already true.
    signature: list[int] | None = None
    fired_at: float | None = None

    def trigger(self, current: list[int] | None) -> str:
        """What happened, or ``""`` when nothing did."""
        if self.on == "missing":
            return "disappeared" if current is None and self.signature is not None else ""
        if current == self.signature:
            return ""
        if current is None:
            return "disappeared"
        return "changed" if self.signature is not None else "appeared"

    def may_fire(self, now: float, *, cooldown_s: float | None = None) -> bool:
        if self.fired_at is None:
            return True
        gap = max(MIN_COOLDOWN_S, cooldown_s if cooldown_s is not None else self.cooldown_s)
        return now - self.fired_at >= gap


class WatchStore:
    """The watches, file-first and owner-only (``state/watches.json``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Watch]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt file must not take the tick down with it. The next write
            # repairs it; until then the agent has no watches, which is a
            # smaller failure than a scheduler that raises every minute.
            log.warning("could not read %s — ignoring the watches it held", self.path)
            return []
        out: list[Watch] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                out.append(Watch(**{**item, "signature": item.get("signature")}))
            except TypeError:
                continue
        return out

    def save(self, watches: list[Watch]) -> None:
        payload = json.dumps([asdict(w) for w in watches], ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def add(
        self,
        *,
        path: str,
        note: str,
        on: str = "change",
        once: bool = True,
        cooldown_s: float = 600.0,
    ) -> Watch:
        watches = self.all()
        if len(watches) >= MAX_WATCHES:
            raise ValueError(f"there are already {MAX_WATCHES} watches — remove one first")
        if on not in ("change", "missing"):
            raise ValueError(f"unknown watch mode {on!r} — change or missing")
        watch = Watch(
            id=f"w{secrets.token_hex(4)}",
            path=str(path),
            note=note.strip(),
            on=on,
            once=bool(once),
            cooldown_s=max(MIN_COOLDOWN_S, float(cooldown_s)),
            signature=signature(Path(path)),
        )
        watches.append(watch)
        self.save(watches)
        return watch

    def put(self, watch: Watch) -> None:
        watches = [w for w in self.all() if w.id != watch.id]
        watches.append(watch)
        self.save(watches)

    def remove(self, watch_id: str) -> bool:
        watches = self.all()
        kept = [w for w in watches if w.id != watch_id]
        if len(kept) == len(watches):
            return False
        self.save(kept)
        return True

    def get(self, watch_id: str) -> Watch | None:
        return next((w for w in self.all() if w.id == watch_id), None)


def describe_fire(watch: Watch, what: str, when: float) -> str:
    """The line delivered when the model declines to say anything.

    A watch is an instruction, so silence is not an outcome it may choose: if
    the model returns nothing, the fact still arrives. This is the plain
    version, and it is always true.
    """
    return (
        f"{watch.note} — {Path(watch.path).name} {what} "
        f"({time.strftime('%H:%M', time.localtime(when))})"
    )


def prompt_for(watch: Watch, what: str) -> str:
    """What the wake composer is asked, when the model is available."""
    return (
        "A watch the owner set has fired — this is not a scheduled wake and not a "
        "reminder; the owner asked to be told about this specific thing.\n\n"
        f"Watch: {watch.note}\n"
        f"Path: {watch.path}\n"
        f"What happened: the path {what}.\n\n"
        "Tell the owner in one or two sentences: what changed, and whether it looks "
        "worth acting on. Do not repeat the path unless it matters — they know what "
        "they asked to watch. If you can cheaply check what the change contains, do."
    )


def any_fire_due(watches: list[Watch], now: float) -> list[tuple[Watch, str, list[int] | None]]:
    """Every watch with something to say right now, with what it saw."""
    due: list[tuple[Watch, str, list[int] | None]] = []
    for watch in watches:
        current = signature(Path(watch.path))
        what = watch.trigger(current)
        if not what or not watch.may_fire(now):
            continue
        due.append((watch, what, current))
    return due
