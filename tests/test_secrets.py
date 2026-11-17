"""The secret store: encrypted at rest, owner-only key, and names that stay inside.

A secret round-trips but never appears in plaintext on disk, the master key is
0600, an unknown name is None rather than an error, and a name is sanitised so a
traversal cannot write outside the store.
"""

from __future__ import annotations

import stat
from pathlib import Path

from daedalus.resources.secrets import SecretStore


def test_a_secret_round_trips_encrypted_under_an_owner_only_key(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "vault")
    store.put("discord", "hunter2-hunter2")
    assert store.get("discord") == "hunter2-hunter2"

    raw = (store.dir / "discord.enc").read_bytes()
    assert b"hunter2" not in raw

    store.ensure_key()
    mode = stat.S_IMODE(store.key_path.stat().st_mode)
    assert mode == 0o600

    # an unknown name is None, not an error
    assert SecretStore(tmp_path / "vault").get("nope") is None

    # the store lists what it holds, and delete removes one
    named = SecretStore(tmp_path / "named")
    named.put("a", "1")
    named.put("b", "2")
    assert named.names() == ["a", "b"]
    assert named.delete("a")
    assert named.names() == ["b"]


def test_a_name_is_sanitised_so_it_cannot_escape_the_store(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "vault")
    store.put("../../escape", "x")
    assert len(list(store.dir.glob("*.enc"))) == 1
    assert not (tmp_path / "escape.enc").exists()
