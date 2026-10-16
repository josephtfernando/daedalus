"""Judge an action's *danger* before the owner is asked to approve it.

The approval prompt used to say `shell` on `shell:*` — internal jargon that
tells the owner nothing about what is about to happen, and treats
`git status --short` and `rm -rf ~` as the same event.

This module is that missing judgement. It is **deterministic on purpose**: an
approval decision must never wait on a model round-trip, and a model's opinion
of its own safety is exactly what SPEC §6 says never to trust.

Levels are about **how bad the worst plausible outcome is**, not how scary the
command looks:

- ``OK``           — reads state; nothing changes.
- ``CAUTION``      — changes something, and the change can be undone.
- ``DESTRUCTIVE``  — destroys data or history; `git checkout` will not bring it back.
- ``CATASTROPHIC`` — can destroy the machine or this install, or reaches data the
                     owner has put out of bounds. Refused outright, never offered
                     as a choice.

The rules are heuristics and are meant to be read and extended. Both error
directions have a cost: a false ``CAUTION`` costs the owner a glance, a false
``OK`` costs them their work, so the unrecognised case is never called safe.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum

# A `rm` aimed at one of these removes the machine's footing. Aimed at
# `~/code/build`, it is merely destructive.
_CATASTROPHIC_TARGETS = frozenset(
    {"/", "/*", "*", "~", "~/", "$HOME", "${HOME}", "..", ".", "/home", "/dev", "/proc", "/sys"}
)

# Removing these, or anything one level inside them, takes the machine with it.
_SYSTEM_ROOTS = frozenset(
    {"/usr", "/etc", "/var", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/root", "/srv"}
)


class Level(str, Enum):
    OK = "ok"
    CAUTION = "caution"
    DESTRUCTIVE = "destructive"
    CATASTROPHIC = "catastrophic"


ORDER: dict[Level, int] = {
    Level.OK: 0,
    Level.CAUTION: 1,
    Level.DESTRUCTIVE: 2,
    Level.CATASTROPHIC: 3,
}

# What the owner reads next to the level. This is the line that decides whether
# they read the rest of the prompt, so it stays flat and plain.
LEVEL_NOTE: dict[Level, str] = {
    Level.OK: "no lasting change",
    Level.CAUTION: "changes something — reversible",
    Level.DESTRUCTIVE: "destructive — this cannot be undone",
    Level.CATASTROPHIC: "refused — unrecoverable, or out of bounds",
}

UNRECOGNISED_NOTE = "not recognised — assume it changes something"


@dataclass(frozen=True, slots=True)
class Assessment:
    """What one action does, and how bad it could get."""

    level: Level = Level.OK
    summary: str = "run a command"
    reasons: tuple[str, ...] = ()
    recognised: bool = True
    target: str = ""
    # An explicit replacement for the level's stock risk line, for the rare case
    # where the level is right for *tiering* but misleads the owner as *prose* —
    # e.g. a destructive op whose call is malformed and would actually do nothing.
    note_text: str = ""

    @property
    def dangerous(self) -> bool:
        return ORDER[self.level] >= ORDER[Level.DESTRUCTIVE]

    @property
    def refused(self) -> bool:
        return self.level is Level.CATASTROPHIC

    def note(self) -> str:
        if self.note_text:
            return self.note_text
        if not self.recognised:
            return UNRECOGNISED_NOTE
        return LEVEL_NOTE[self.level]


# Paths the owner has put out of bounds (AGENTS.md, SPEC §6): secrets, keys,
# private notes. `files` already refuses these; `shell` could walk straight
# through that refusal, which is what makes a guard on one tool worth nothing.
PROTECTED_PATH = (
    r"((~|/home/[a-z0-9_.-]+)?/\.(ssh|aws|gnupg|netrc|dockercfg)\b"
    r"|(^|[\s/\"'=])\.env(\b|$)"
    r"|master\.key"
    r"|docs/private"
    r"|/(private|vault)/)"
)

# (level, pattern, what it does, why it is flagged)
_RULES: tuple[tuple[Level, str, str, str], ...] = (
    # -- refused outright -------------------------------------------------
    (
        Level.CATASTROPHIC,
        r":\s*\(\s*\)\s*\{.*\}\s*;?\s*:",
        "run a fork bomb",
        "spawns processes until the machine stops responding",
    ),
    (
        Level.CATASTROPHIC,
        r"\b(mkfs(\.[a-z0-9]+)?|fdisk|parted|wipefs|sgdisk)\b",
        "format or repartition a disk",
        "destroys a filesystem",
    ),
    (
        Level.CATASTROPHIC,
        r"\bdd\b[^|;&]*\bof=/dev/",
        "write raw bytes to a device",
        "overwrites a disk",
    ),
    (
        Level.CATASTROPHIC,
        r">\s*/dev/(sd|nvme|hd|vd|mmcblk)",
        "write raw bytes to a device",
        "overwrites a disk",
    ),
    (
        Level.CATASTROPHIC,
        PROTECTED_PATH,
        "touch a path that is out of bounds",
        "secrets and private notes are never opened, read, or written",
    ),
    # -- destructive ------------------------------------------------------
    (
        Level.DESTRUCTIVE,
        r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*[fd]|push\b[^|;&]*(--force\b|\s-f\b)"
        r"|branch\s+-D|checkout\s+--\s|restore\b[^|;&]*--source)",
        "rewrite or discard git history",
        "committed or uncommitted work can be lost",
    ),
    (
        Level.DESTRUCTIVE,
        r"\b(rm|shred|unlink)\b",
        "delete files",
        "deleted data does not come back",
    ),
    (
        Level.DESTRUCTIVE,
        r"\btruncate\b[^|;&]*-s\s*0\b",
        "empty a file",
        "its previous contents are gone",
    ),
    (
        Level.DESTRUCTIVE,
        r"\b(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b",
        "run code fetched from the network",
        "the code is invisible until it is already running",
    ),
    (
        Level.DESTRUCTIVE,
        r"\b(chmod|chown)\s+-[a-z]*R",
        "change permissions or ownership, recursively",
        "can lock the owner out of their own files",
    ),
    (
        Level.DESTRUCTIVE,
        r"\b(shutdown|reboot|poweroff|halt)\b",
        "power the machine off",
        "stops everything that is running",
    ),
    (
        Level.DESTRUCTIVE,
        r"\b(pkill|killall|kill)\b[^|;&]*(-9\b|-KILL\b|-SIGKILL\b)",
        "force-kill processes",
        "they get no chance to save state",
    ),
    # -- caution ----------------------------------------------------------
    (
        Level.CAUTION,
        r"\bsudo\b",
        "run a command as root",
        "runs with full privileges",
    ),
    (
        Level.CAUTION,
        r"\bsystemctl\b[^|;&]*\b(start|stop|restart|reload|try-restart|enable|disable|mask"
        r"|unmask|poweroff|reboot|suspend|hibernate|kill|daemon-reload)\b",
        "control a system service",
        "changes what is running on the machine",
    ),
    (
        Level.CAUTION,
        r"\b(pip|pip3|uv|npm|pnpm|yarn|apt|apt-get|brew|pacman|dnf|cargo)\b[^|;&]*"
        r"\b(install|add|remove|uninstall|upgrade|sync)\b",
        "install or remove software",
        "changes what is on the machine",
    ),
    (
        Level.CAUTION,
        r"(^|[|;&]\s*)(mv|cp|mkdir|touch|tee|ln)\b",
        "move, copy, or create files",
        "files change on disk",
    ),
    (
        # A file descriptor redirect (`2>/dev/null`) is not an overwrite.
        Level.CAUTION,
        r"(?<![0-9])>(?!>|=)\s*(?!/dev/null\b)(?!/dev/stderr\b)[^|&\s]",
        "overwrite a file",
        "its previous contents are replaced",
    ),
    (
        Level.CAUTION,
        r"\bgit\s+(commit|add|merge|rebase|cherry-pick|stash|apply|tag)\b",
        "change the git repository",
        "writes to the repo",
    ),
    # -- read-only --------------------------------------------------------
    (
        Level.OK,
        r"^\s*(ls|cat|head|tail|wc|stat|file|which|whoami|id|date|pwd|env|printenv|echo|cd"
        r"|df|du|free|uptime|ps|top|hostname|uname|nproc|lsof|ss|ip|ping|dig|nslookup"
        r"|rg|grep|find|fd|jq|sort|uniq|cut|column|diff|basename|dirname|realpath|readlink"
        r"|git\s+(status|log|diff|show|branch|remote|rev-parse|describe|blame|tag\s+-l)"
        r"|systemctl\s+(-{1,2}[a-z0-9-]+\s+)*(status|is-active|is-enabled|show|list-units|list-timers|cat)"
        r"|sed\s+-n"
        r")\b",
        "read state",
        "",
    ),
)

_COMPILED = tuple((level, re.compile(pat), what, why) for level, pat, what, why in _RULES)

# `cd somewhere && …` is navigation, not an action. Judge what follows it.
_CD_PREFIX = re.compile(r"^\s*cd\s+[^&;|]+\s*(?:&&|;)\s*")


def _catastrophic_rm(text: str) -> str:
    """Return the target if this command recursively removes the machine's footing."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for index, token in enumerate(tokens):
        if token.rsplit("/", 1)[-1] != "rm":
            continue
        targets = [t for t in tokens[index + 1 :] if not t.startswith("-")]
        for target in targets:
            if target in _CATASTROPHIC_TARGETS:
                return target
            norm = target.rstrip("/")
            if not norm:
                continue
            if norm in _CATASTROPHIC_TARGETS or norm in _SYSTEM_ROOTS:
                return target
            parts = [p for p in norm.split("/") if p]
            # One level inside a system root (`/usr/lib`) takes the machine with
            # it; deeper (`/usr/local/myapp`) is merely destructive.
            if len(parts) <= 2 and f"/{parts[0]}" in _SYSTEM_ROOTS:
                return target
    return ""


def classify_command(command: str) -> Assessment:
    """Judge one shell command. Never raises."""
    text = command.replace("\n", " ").strip()
    if not text:
        return Assessment(summary="run an empty command")

    peeled = _CD_PREFIX.sub("", text, count=1)
    if peeled != text and peeled:
        return classify_command(peeled)

    worst: Level | None = None
    summary = ""
    reasons: list[str] = []

    wiped = _catastrophic_rm(text)
    if wiped:
        worst = Level.CATASTROPHIC
        summary = f"delete {wiped} and everything under it"
        reasons.append("recursive delete of a directory the machine lives in")

    for level, pattern, what, why in _COMPILED:
        if not pattern.search(text):
            continue
        # `worst is None` matters: an OK-level match is a real finding, and the
        # first one has to be recorded rather than compared against a default.
        if worst is None or ORDER[level] > ORDER[worst]:
            worst = level
            summary = what
        if why and ORDER[level] >= ORDER[Level.DESTRUCTIVE]:
            reasons.append(why)

    if worst is None:
        # Nothing matched. The command is not recognisably read-only, so we do
        # not get to call it safe — but we also should not cry wolf: say plainly
        # that it was not recognised.
        return Assessment(
            level=Level.CAUTION,
            summary="run a command that this classifier does not recognise",
            reasons=("it did not match the known read-only commands",),
            recognised=False,
        )
    return Assessment(
        level=worst,
        summary=summary,
        reasons=tuple(dict.fromkeys(reasons)),
        target=wiped,
    )
