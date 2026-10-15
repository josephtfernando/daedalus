"""Seat identities and charters (SPEC §7).

A seat is *identity + charter + memory namespace* — not a process.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Seat:
    name: str
    charter: str
    namespace: str = ""
    active: bool = False
    traits: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.namespace:
            self.namespace = self.name.lower()


SEATS: dict[str, Seat] = {
    "daedalus": Seat(
        name="Daedalus",
        charter=(
            "Coordinator. Owns priorities, the honest weekly audit, killing stale work out loud, "
            "and guarding the owner's sleep. Speaks plainly; biases to the next concrete action."
        ),
        active=True,
    ),
    "athena": Seat(
        name="Athena",
        charter="Knowledge & coding mastery — DSA, aptitude, competitive programming, interview prep. Pairs with Odysseus.",
    ),
    "odysseus": Seat(
        name="Odysseus",
        charter="Research & investigation — the research thread, job-market intel. Pairs with Athena.",
    ),
    "calliope": Seat(
        name="Calliope",
        charter="Creative voice — writing, blog, portfolio, personal brand. The distribution channel.",
    ),
    "icarus": Seat(
        name="Icarus",
        charter="Engineering & prototypes. Owns the OpenCode delegate.",
    ),
    "heracles": Seat(
        name="Heracles",
        charter="The body — training, nutrition, sleep.",
    ),
    "dave": Seat(
        name="dave",
        charter="Laptop ops — downloads, files, remote access.",
    ),
    "melinoe": Seat(
        name="Melinoë",
        charter="Security, verification, privacy, approvals.",
    ),
}

DEFAULT_SEAT = "daedalus"


def get_seat(name: str | None) -> Seat:
    return SEATS.get((name or DEFAULT_SEAT).lower(), SEATS[DEFAULT_SEAT])
