"""Encrypted credential store (SPEC §6 "bytes stay home", ARCHITECTURE §10).

Credentials are encrypted at rest with a per-install master key kept at
``resources/master.key`` (0600). The plaintext never touches a log, a prompt,
or Discord.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.dir = home / "resources" / "creds"
        self.key_path = home / "resources" / "master.key"
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(home / "resources", 0o700)
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    # -- key --------------------------------------------------------------

    def ensure_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return key

    def _fernet(self) -> Fernet:
        return Fernet(self.ensure_key())

    # -- values -----------------------------------------------------------

    def _path(self, name: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
        return self.dir / f"{safe}.enc"

    def put(self, name: str, value: str) -> None:
        path = self._path(name)
        path.write_bytes(self._fernet().encrypt(value.encode("utf-8")))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return self._fernet().decrypt(path.read_bytes()).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.enc"))

    def has(self, name: str) -> bool:
        return self._path(name).exists()

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False
