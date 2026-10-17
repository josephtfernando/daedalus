"""The conversation transcript: what it keeps, where, and with what permissions."""

from __future__ import annotations

import json
import os
import stat

from daedalus.loop.transcript import MAX_MESSAGES, Transcript


def test_a_transcript_round_trips(tmp_path) -> None:
    store = Transcript(tmp_path)
    store.write("owner", [{"role": "user", "content": "hello"}])

    assert store.read("owner") == [{"role": "user", "content": "hello"}]


def test_an_unknown_conversation_reads_as_empty(tmp_path) -> None:
    assert Transcript(tmp_path).read("nobody") == []


def test_a_transcript_is_trimmed_to_the_window(tmp_path) -> None:
    """A conversation that runs for a year must not grow a file for a year."""
    store = Transcript(tmp_path)
    store.write("owner", [{"role": "user", "content": str(n)} for n in range(MAX_MESSAGES * 2)])

    kept = store.read("owner")
    assert len(kept) == MAX_MESSAGES
    assert kept[-1]["content"] == str(MAX_MESSAGES * 2 - 1), "the newest message was dropped"


def test_the_files_are_owner_only(tmp_path) -> None:
    """The owner's own words are in here; nobody else on the box reads them."""
    store = Transcript(tmp_path)
    store.write("owner", [{"role": "user", "content": "a private plan"}])

    mode = stat.S_IMODE(os.stat(tmp_path / "owner.json").st_mode)
    assert mode == 0o600, f"the transcript is {oct(mode)}, not owner-only"


def test_a_conversation_key_cannot_escape_the_directory(tmp_path) -> None:
    """A key comes from a surface id; it is never trusted as a path."""
    store = Transcript(tmp_path)
    store.write("../../etc/passwd", [{"role": "user", "content": "nope"}])

    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path, "a conversation escaped the transcript directory"


def test_a_corrupt_transcript_is_ignored_not_fatal(tmp_path) -> None:
    """Half a file must not take a turn down with it."""
    (tmp_path / "owner.json").write_text("{ this is not json", encoding="utf-8")

    assert Transcript(tmp_path).read("owner") == []


def test_a_transcript_that_is_not_a_list_is_ignored(tmp_path) -> None:
    (tmp_path / "owner.json").write_text(json.dumps({"role": "user"}), encoding="utf-8")

    assert Transcript(tmp_path).read("owner") == []


def test_an_empty_key_keeps_no_file(tmp_path) -> None:
    store = Transcript(tmp_path)
    store.write("", [{"role": "user", "content": "orphan"}])

    assert list(tmp_path.iterdir()) == []
