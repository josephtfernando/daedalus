"""The conversation transcript: what it keeps, where, and with what permissions."""

from __future__ import annotations

import json
import os
import stat

from daedalus.loop.transcript import MAX_MESSAGES, Transcript


def test_a_transcript_round_trips_and_keeps_only_the_newest_window(tmp_path) -> None:
    """A conversation that runs for a year must not grow a file for a year."""
    # case: what was written is what is read.
    store = Transcript(tmp_path)
    store.write("owner", [{"role": "user", "content": "hello"}])
    assert store.read("owner") == [{"role": "user", "content": "hello"}]

    # case: an unknown conversation reads as empty, not as an error.
    assert Transcript(tmp_path).read("nobody") == []

    # case: a long conversation is trimmed to the window, newest kept.
    store.write("owner", [{"role": "user", "content": str(n)} for n in range(MAX_MESSAGES * 2)])
    kept = store.read("owner")
    assert len(kept) == MAX_MESSAGES
    assert kept[-1]["content"] == str(MAX_MESSAGES * 2 - 1), "the newest message was dropped"


def test_transcripts_are_owner_only_and_cannot_escape_their_directory(tmp_path) -> None:
    """The owner's own words are in here; a key from a surface is never a path."""
    # case: the file is owner-only.
    store = Transcript(tmp_path)
    store.write("owner", [{"role": "user", "content": "a private plan"}])
    mode = stat.S_IMODE(os.stat(tmp_path / "owner.json").st_mode)
    assert mode == 0o600, f"the transcript is {oct(mode)}, not owner-only"

    # case: a traversal key stays inside the transcript directory.
    before = set(tmp_path.glob("*.json"))
    store.write("../../etc/passwd", [{"role": "user", "content": "nope"}])
    new = set(tmp_path.glob("*.json")) - before
    assert len(new) == 1
    assert next(iter(new)).parent == tmp_path, "a conversation escaped the transcript directory"

    # case: an empty key keeps no file at all.
    store.write("", [{"role": "user", "content": "orphan"}])
    assert set(tmp_path.glob("*.json")) == before | new


def test_a_corrupt_transcript_is_ignored_not_fatal(tmp_path) -> None:
    """Half a file, or the wrong shape, must not take a turn down with it."""
    (tmp_path / "owner.json").write_text("{ this is not json", encoding="utf-8")
    assert Transcript(tmp_path).read("owner") == []

    (tmp_path / "owner.json").write_text(json.dumps({"role": "user"}), encoding="utf-8")
    assert Transcript(tmp_path).read("owner") == []
