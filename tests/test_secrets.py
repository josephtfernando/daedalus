from __future__ import annotations

import os
import stat
from pathlib import Path

from daedalus.resources.secrets import SecretStore


def test_round_trip_and_encryption_at_rest(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.put("discord", "hunter2-hunter2")
    assert store.get("discord") == "hunter2-hunter2"
    raw = (store.dir / "discord.enc").read_bytes()
    assert b"hunter2" not in raw


def test_master_key_is_owner_only(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.ensure_key()
    mode = stat.S_IMODE(store.key_path.stat().st_mode)
    assert mode == 0o600


def test_missing_secret_is_none(tmp_path: Path) -> None:
    assert SecretStore(tmp_path).get("nope") is None


def test_names_and_delete(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.put("a", "1")
    store.put("b", "2")
    assert store.names() == ["a", "b"]
    assert store.delete("a")
    assert store.names() == ["b"]


def test_name_is_sanitised(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.put("../../escape", "x")
    assert len(list(store.dir.glob("*.enc"))) == 1
    assert not (tmp_path / "escape.enc").exists()
