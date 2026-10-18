"""Authoring: materialise a capability on disk.

The *model* supplies the artifact; the *engine* owns where it lands and how it
is registered (ARCHITECTURE §11). No hand-coded feature is required.
"""

from __future__ import annotations

from pathlib import Path

from .manifest import Manifest

CAPABILITY_TEMPLATE = '''"""Authored capability: {name}

{description}

Protocol: read JSON on stdin ({{args, context, state}}), write JSON on stdout
({{result, state_next, notifications}}). Keep it deterministic — a scheduled run
must execute this file with no model in the loop.
"""
import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{{}}")
    args = payload.get("args", {{}})
    state = payload.get("state", {{}})

    # --- the capability's logic -----------------------------------------
    result = {{"echo": args}}
    state_next = state
    notifications = []
    # --------------------------------------------------------------------

    json.dump(
        {{"result": result, "state_next": state_next, "notifications": notifications}},
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

README_TEMPLATE = """# {name}

{description}

Why it exists: {why}

## Contract

- Input: `{input_schema}`
- Output: `{output_schema}`

## Notes

- Scopes: `{scopes}`
- Schedule: `{schedule}`
"""


def materialize(
    root: Path,
    manifest: Manifest,
    *,
    run_py: str,
    readme: str | None = None,
    why: str = "",
    state: dict | None = None,
) -> Path:
    """Write ``capabilities/<name>/{run.py, manifest.toml, README.md, state/}``."""
    cap_dir = root / manifest.name
    (cap_dir / "state").mkdir(parents=True, exist_ok=True)
    entry = cap_dir / manifest.entry
    entry.write_text(run_py, encoding="utf-8")
    # Executable at birth (T41). The limits launcher `execvp`s the entry file
    # directly — the launcher's caps survive the exec — and a file `write_text`
    # creates is 0644, so every capability was born with a 127 waiting for its
    # first scheduled run: a failure indistinguishable from success.
    entry.chmod(0o755)
    manifest.save(cap_dir)
    (cap_dir / "README.md").write_text(
        readme
        or README_TEMPLATE.format(
            name=manifest.name,
            description=manifest.description,
            why=why or "authored on request",
            input_schema=manifest.io.get("input_schema", {}),
            output_schema=manifest.io.get("output_schema", {}),
            scopes=manifest.scopes.to_dict(),
            schedule=manifest.schedule or "manual",
        ),
        encoding="utf-8",
    )
    return cap_dir
