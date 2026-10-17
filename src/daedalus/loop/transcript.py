"""The running exchange per conversation, kept so a restart is not amnesia.

The prompt already carried a window of what was said — but only from
``Runtime.history``, a dict that died with the process, and only for turns that
happened to end in prose. A turn the repeat guard cut short, a turn the step
limit cut short, a turn where the owner denied an action: none of those were
recorded, so the *next* turn read a conversation that had never contained the
owner's last message.

That is the amnesia the acceptance run showed. The owner asked for the same
change four times; each turn saw a conversation that stopped before it started.
The daemon was restarted mid-conversation too, which wiped even the turns that
had been recorded.

So the last few exchanges are written down here, one file per conversation,
under ``state/history/``. It is a **transcript**, not memory: facts belong in
the memory facets, where they can be superseded, classified and searched. This
is only "what did we just say", bounded to the recent past, and it is replayed
into the prompt exactly as the in-memory window was.

The owner's own words live here, so the directory is owner-only and the files
are written ``0600``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("daedalus.transcript")

#: How many messages one conversation keeps. Matches the window the prompt
#: replays (``context.build_messages(max_history=12)``) with room to spare, so a
#: slightly wider window is available without rewriting every file.
MAX_MESSAGES = 24


class Transcript:
    def __init__(self, root: Path, *, limit: int = MAX_MESSAGES) -> None:
        self.root = root
        self.limit = limit

    def read(self, key: str) -> list[dict[str, Any]]:
        """The last few messages of a conversation, or ``[]`` if there is none.

        A transcript that cannot be read is not worth failing a turn over: the
        owner loses the context of a sentence, not the work. It is logged at
        debug and treated as absent.
        """
        path = self._path(key)
        if path is None or not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.debug("could not read the transcript for %r", key, exc_info=True)
            return []
        if not isinstance(data, list):
            return []
        return [m for m in data if isinstance(m, dict)][-self.limit :]

    def write(self, key: str, thread: list[dict[str, Any]]) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # Write to a neighbour and move it into place, so a crash mid-write
            # cannot leave a half-file behind for the next turn to read.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(thread[-self.limit :], ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError:
            log.warning("could not write the transcript for %r", key, exc_info=True)

    def _path(self, key: str) -> Path | None:
        """A filesystem-safe name for a conversation key.

        The key is a Discord id — digits — but a caller may hand over anything,
        so anything that is not obviously safe is folded to ``_``. An empty key
        has no conversation to keep and gets no file.
        """
        stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in key).strip("._")
        if not stem:
            return None
        return self.root / f"{stem[:120]}.json"
