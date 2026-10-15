"""Workspace guard — reach narrow (SPEC §6, AGENTS.md).

Local work reads the minimum needed. Two rules are enforced here, in code:

* a path must live under an allowed root (the state home, the code repo, cwd);
* a path may never be one of the always-blocked ones (``.env``, ``docs/private``,
  the Obsidian ``private/`` folder, SSH keys).
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..storage import paths

BLOCKED_SUBSTRINGS = (
    "/.env",
    "/.ssh/",
    "id_rsa",
    "id_ed25519",
    "/docs/private/",
    "/private/obsidian",
)


class OutOfBounds(PermissionError):
    """Raised when a path is outside the permitted workspace."""


def allowed_roots(settings: Settings) -> list[Path]:
    roots: list[Path] = [settings.home, paths.repo_root(), Path.cwd()]
    extra = (settings.raw.get("files", {}) or {}).get("roots", []) or []
    roots.extend(Path(p).expanduser() for p in extra)
    out: list[Path] = []
    for r in roots:
        try:
            out.append(r.resolve())
        except OSError:
            continue
    return out


def is_blocked(path: Path) -> bool:
    text = "/" + str(path).replace("\\", "/").lstrip("/")
    if path.name == ".env" or path.name.startswith(".env."):
        return True
    return any(marker in text for marker in BLOCKED_SUBSTRINGS)


def resolve(raw: str, settings: Settings, *, base: Path | None = None) -> Path:
    """Resolve a user-supplied path, enforcing root and block rules."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    try:
        p = p.resolve()
    except OSError as exc:
        raise OutOfBounds(f"cannot resolve {raw!r}: {exc}") from exc
    if is_blocked(p):
        raise OutOfBounds(f"refusing to touch a protected path: {raw!r}")
    roots = allowed_roots(settings)
    if not any(_under(p, root) for root in roots):
        raise OutOfBounds(f"path is outside the permitted workspace: {raw!r}")
    return p


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
