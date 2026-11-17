"""The usage ledger: structure only, owner-only, and bounded.

The file answers "which model is slow" and "when do turns overflow" — but only
if it stays readable, private, and small. A daemon writes one line per turn
forever; unbounded growth is how a diagnostic becomes a disk problem.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from daedalus.loop import usage as usage_mod
from daedalus.loop.usage import record_usage, usage_path


def test_the_usage_file_is_owner_only_even_when_it_arrived_loose(tmp_path: Path) -> None:
    """The owner's activity is in here; a restore must not leave it readable."""
    # case: a fresh file is created 0600.
    home = tmp_path / "home"
    record_usage(home, {"seat": "daedalus"})
    assert stat.S_IMODE(usage_path(home).stat().st_mode) == 0o600

    # case: a file from an older build, or a restore, is tightened on write.
    path = usage_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    record_usage(home, {"seat": "daedalus"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_usage_file_rotates_instead_of_growing_forever(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(usage_mod, "MAX_USAGE_BYTES", 200)
    home = tmp_path / "home"

    for turn in range(12):
        record_usage(home, {"turn": turn})

    path = usage_path(home)
    backup = path.with_name(path.name + ".1")
    assert backup.exists(), "the old generation is kept once"
    assert path.exists(), "and a fresh file takes the new lines"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    for file in (path, backup):
        for line in file.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["turn"] >= 0
