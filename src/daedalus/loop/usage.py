"""One line per turn: what it cost in tokens and seconds (pi's usage ledger).

Pi records model-attributed usage for every response and prices it per session.
Daedalus runs on free tiers, so there is nothing to price — but *tokens and
time* are still the two numbers that answer "which provider is slow", "when do
turns start overflowing", and "did this wake cost a full turn to say one line".
Until now ``ChatResult.usage`` was parsed and dropped on the floor.

The record is **structure only** — seat, provider, steps, tokens, seconds,
retries, whether the turn ended in an error. Never the owner's words, never a
result: the same rule the loop trace follows. It lives under ``state/`` beside
the transcripts and the rate-limit ledger, and it is written ``0600``.

It is also a file that only ever grows, one line per turn, forever. A daemon
that runs for a year writes a year of lines; nothing reads them back at that
size. So the file is capped: past the cap the whole file becomes ``.1`` (one
generation kept) and a fresh file starts. One rotated generation is enough to
answer "what changed this month" without the file becoming its own problem.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("daedalus.usage")

#: Rotate at 5 MiB — roughly a quarter-million turns at ~200 bytes each, so a
#: normal year of use never rotates and a pathological one cannot fill a disk.
MAX_USAGE_BYTES = 5 * 1024 * 1024


def usage_path(home: Path) -> Path:
    return home / "state" / "usage.jsonl"


def _rotate(path: Path) -> None:
    """Move the current file aside once it is past the cap (one generation)."""
    try:
        if not path.exists() or path.stat().st_size < MAX_USAGE_BYTES:
            return
        backup = path.with_name(path.name + ".1")
        backup.unlink(missing_ok=True)
        path.rename(backup)
        os.chmod(backup, 0o600)
    except OSError:
        # A rotation that fails must not cost the line we were about to write.
        log.warning("could not rotate the usage ledger", exc_info=True)


def record_usage(home: Path, entry: dict[str, Any]) -> None:
    """Append one turn's usage. Never fatal: a lost statistic is not a lost turn."""
    path = usage_path(home)
    line = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        # Every write, not only the first: a file created by an older build, or
        # restored from a backup, must not keep a looser mode just because it
        # already existed.
        os.chmod(path, 0o600)
    except OSError:
        log.warning("could not record turn usage", exc_info=True)
