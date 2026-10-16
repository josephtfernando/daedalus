"""Seat identities and charters (SPEC §7).

A seat is *identity + charter + memory namespace* — not a process.

The charters are **files**: ``$DAEDALUS_HOME/cabinet/seats/<key>.toml``. A charter
is the thing the owner will most often want to say differently — "not that, more
like this" — and a charter that can only be changed by editing Python is a charter
nobody changes. So the eight charters below are the **fallback**, not the source:
they are written out on first load, an edited file wins, and a file that is
deleted falls back to its built-in rather than to nothing.

Two rules keep the file-first arrangement honest:

- **A malformed charter is reported, not swallowed.** It names the file, the
  problem, and what the file may contain. A seat that quietly loses its charter
  is a seat whose answers the owner cannot explain.
- **The set is closed.** An unknown key in a charter file is refused rather than
  ignored, because a typo'd `charterr` that leaves the built-in in place is the
  one failure the owner would never notice — the prompt still builds, and it
  still says the old thing.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "BUILTIN",
    "CHART_FIELDS",
    "DEFAULT_SEAT",
    "CharterError",
    "Seat",
    "builtin_charters",
    "charter_dir",
    "charter_file",
    "load_charters",
    "pick",
    "write_charter",
]

#: The seat every turn belongs to unless the owner says otherwise.
DEFAULT_SEAT = "daedalus"

#: What a charter file may say. Anything else is a typo, and a typo that is
#: ignored is worse than one that is refused.
CHART_FIELDS = frozenset({"name", "charter", "namespace", "active", "traits"})

#: The eight built-in charters (SPEC §7), as the fallback for a fresh install.
_BUILTIN: tuple[tuple[str, str, str], ...] = (
    (
        "daedalus",
        "Daedalus",
        "Coordinator. Owns priorities, the honest weekly audit, killing stale work out loud, "
        "and guarding the owner's sleep. Speaks plainly; biases to the next concrete action.",
    ),
    (
        "athena",
        "Athena",
        "Knowledge & coding mastery — DSA, aptitude, competitive programming, interview prep. "
        "Pairs with Odysseus.",
    ),
    (
        "odysseus",
        "Odysseus",
        "Research & investigation — the research thread, job-market intel. Pairs with Athena.",
    ),
    (
        "calliope",
        "Calliope",
        "Creative voice — writing, blog, portfolio, personal brand. The distribution channel.",
    ),
    (
        "icarus",
        "Icarus",
        "Engineering & prototypes. Owns the OpenCode delegate.",
    ),
    (
        "heracles",
        "Heracles",
        "The body — training, nutrition, sleep.",
    ),
    (
        "dave",
        "dave",
        "Laptop ops — downloads, files, remote access.",
    ),
    (
        "melinoe",
        "Melinoë",
        "Security, verification, privacy, approvals.",
    ),
)


class CharterError(ValueError):
    """A charter file that cannot be believed, named by its path."""


@dataclass(slots=True)
class Seat:
    name: str
    charter: str
    key: str = ""
    namespace: str = ""
    active: bool = False
    traits: list[str] = field(default_factory=list)
    #: Where the charter came from, for `daedalus seats` to say. Empty means the
    #: built-in — which is not the same as "no charter", it is "the one in the code".
    source: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.name.lower()
        if not self.namespace:
            self.namespace = self.key

    @property
    def from_file(self) -> bool:
        return bool(self.source)


#: The fallback charters, by key. Active is the default seat and only the default
#: seat: exactly one seat is the one the owner is talking to (SPEC §7).
BUILTIN: dict[str, Seat] = {
    key: Seat(key=key, name=name, charter=charter, active=key == DEFAULT_SEAT)
    for key, name, charter in _BUILTIN
}


def builtin_charters() -> dict[str, Seat]:
    """Fresh copies of the eight built-ins — the fallback, safe to mutate."""
    return {key: replace(seat) for key, seat in BUILTIN.items()}


def charter_dir(home: Path) -> Path:
    """Where the owner's charters live: ``<home>/cabinet/seats``."""
    return Path(home) / "cabinet" / "seats"


def charter_file(home: Path, key: str) -> Path:
    return charter_dir(home) / f"{key}.toml"


def load_charters(home: Path, *, seed: bool = True) -> dict[str, Seat]:
    """The eight seats, with every charter file the owner has taken over.

    With ``seed`` (the default) a charter file is written for any seat that does
    not have one, so the directory the owner would edit always exists and always
    holds something to edit. Seeding never overwrites: a file that is there is
    the owner's.
    """
    directory = charter_dir(home)
    if seed:
        directory.mkdir(parents=True, exist_ok=True)
        for key, seat in BUILTIN.items():
            if not charter_file(home, key).exists():
                write_charter(home, seat)
    taken: dict[str, Seat] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.toml")):
            seat = read_charter(path)
            taken[path.stem.lower()] = seat

    charters = builtin_charters()
    for key, seat in taken.items():
        # A file may add a seat the code does not know: §13 leaves the count
        # open, and a hand-written ninth should work.
        charters[key] = replace(seat, key=key)

    active = sorted(key for key, seat in charters.items() if seat.active)
    if len(active) > 1:
        raise CharterError(
            "two seats are active at once: "
            + ", ".join(active)
            + " — the owner talks to one identity per turn (SPEC §7). Leave "
            "`active = true` on one charter, or on none and the default stands."
        )
    if not active:
        # No charter claims the owner, so the default does. A turn always has an
        # identity; "which seat am I" cannot be left unanswered.
        charters[DEFAULT_SEAT].active = True
    return charters


def read_charter(path: Path) -> Seat:
    """One charter file, or a :class:`CharterError` that says what is wrong."""
    try:
        raw: Any = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CharterError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise CharterError(f"{path}: {exc}") from exc

    unknown = sorted(set(raw) - CHART_FIELDS)
    if unknown:
        raise CharterError(
            f"{path}: unknown {', '.join(unknown)} — a charter file holds "
            f"{', '.join(sorted(CHART_FIELDS))}"
        )
    name = raw.get("name")
    charter = raw.get("charter")
    if not isinstance(name, str) or not isinstance(charter, str) or not name.strip() or not charter.strip():
        raise CharterError(
            f"{path}: a seat needs a `name` and a `charter` — it has "
            f"{sorted(raw)}. Both are quoted strings; the charter is the sentence "
            "or two the seat answers from."
        )
    name, charter = name.strip(), charter.strip()
    active = raw.get("active", False)
    if not isinstance(active, bool):
        raise CharterError(f"{path}: `active` is true or false, not {active!r}")
    traits = raw.get("traits", [])
    if not isinstance(traits, list) or any(not isinstance(t, str) for t in traits):
        raise CharterError(f"{path}: `traits` is a list of strings, not {traits!r}")
    return Seat(
        name=name,
        charter=charter,
        key=Path(path).stem.lower(),
        namespace=str(raw.get("namespace") or "").strip(),
        active=active,
        traits=list(traits),
        source=str(path),
    )


def write_charter(home: Path, seat: Seat) -> Path:
    """Write one charter out as TOML. Used to seed a fresh install.

    Hand-rolled rather than a TOML writer dependency: five fields, written in a
    fixed order, is not worth a library. Strings go through ``json.dumps``, which
    escapes exactly what a TOML basic string escapes.
    """
    path = charter_file(home, seat.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {seat.name} — one of the eight seats (SPEC §7). Edit freely: this file",
        "# is the charter, and it is what the seat answers from. A broken file is",
        "# reported at startup rather than quietly ignored.",
        f"name = {_string(seat.name)}",
        f"charter = {_string(seat.charter)}",
    ]
    if seat.namespace and seat.namespace != seat.key:
        lines.append(f"namespace = {_string(seat.namespace)}")
    if seat.active:
        lines.append("active = true")
    if seat.traits:
        lines.append(f"traits = [{', '.join(_string(t) for t in seat.traits)}]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def pick(charters: Mapping[str, Seat], name: str | None) -> Seat:
    """The seat a name refers to. Never nothing — a turn always has an identity.

    An unknown name is not an error worth stopping a turn for: the owner's phrase
    is honest enough ("Daedalus, ...") and the default seat is the seat that
    already carries the whole system in its head.
    """
    key = str(name or DEFAULT_SEAT).strip().lower()
    return charters.get(key) or charters[DEFAULT_SEAT]


def _string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)
