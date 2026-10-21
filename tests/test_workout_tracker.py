"""The workout tracker (T38): the owner's own record, inside the owner's own bounds.

The decision is the test's subject as much as the tracker is. The owner
adjusted the strictest draft in all three offered directions (19 Oct): the
tracker MAY store values locally with its report readable by owner and
Heracles; he MAY read designated files; the local chain is fine for his
turns. Nothing remote, ever. The acceptance's egress clause — "no remote
model call carries a workout value, checked as a property of the capability's
egress rather than trusted to its prompt" — is held as a structural property:
the manifest declares no network scope, and this file's own imports cannot
fetch. Both are asserted, because a posture trusted to prose is a posture.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from daedalus.capabilities.manifest import Manifest
from daedalus.capabilities.runner import CapabilityRunner


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    source = Path.home() / ".daedalus" / "capabilities" / "workout_tracker"
    dest = tmp_path / "workout_tracker"
    dest.mkdir(parents=True)
    (dest / "run.py").write_text((source / "run.py").read_text(encoding="utf-8"), encoding="utf-8")
    manifest = Manifest.from_path(source / "manifest.toml")
    manifest.save(dest)
    return tmp_path


async def run_cap(root: Path, args: dict, state: dict | None = None) -> tuple[dict, dict, list]:
    manifest = Manifest.from_path(root / "workout_tracker" / "manifest.toml")
    out = await CapabilityRunner(root, use_venv=False).run(manifest, args=args, state=state)
    assert out.ok, out.error or out.stdout
    return out.result, out.state_next, out.notifications


# -- the egress property, before anything else ---------------------------------


async def test_the_egress_is_structural_not_trusted_to_prose(root: Path) -> None:
    """T38's own clause: no remote model call can carry a workout value."""
    manifest = Manifest.from_path(root / "workout_tracker" / "manifest.toml")
    net = manifest.scopes.to_dict().get("net")
    assert not net, f"the manifest declares net={net!r} — a workout capability that can reach out is a leak by declaration"

    source = (root / "workout_tracker" / "run.py").read_text(encoding="utf-8")
    for forbidden in ("import urllib", "import http", "import socket", "import requests", "urlopen", "httpx"):
        assert forbidden not in source, (
            f"{forbidden!r} in run.py — the egress must be a property of the code, "
            "not an intention of the prompt"
        )


# -- recording: values in, one row per session ---------------------------------


async def test_a_session_is_stored_with_its_values(root: Path) -> None:
    result, state, _ = await run_cap(
        root, {"action": "record", "what": "Back Squat", "how_much": "5x5 @ 80kg"}
    )
    assert result["ok"]
    (row,) = state["sessions"].values()
    assert row["how_much"] == "5x5 @ 80kg", "the value is the record — stored, not paraphrased"


async def test_the_same_session_twice_is_a_correction_said_aloud(root: Path) -> None:
    _, state, _ = await run_cap(
        root, {"action": "record", "what": "Back Squat", "how_much": "5x5 @ 80kg"}
    )
    again, state2, _ = await run_cap(
        root, {"action": "record", "what": "Back Squat", "how_much": "3x8 @ 70kg"}, state=state
    )
    assert again["ok"] and any("stands as" in w for w in again["warnings"]), (
        "one session, one row — the correction says what it corrected"
    )
    assert len(state2["sessions"]) == 1 and list(state2["sessions"].values())[0]["how_much"] == "3x8 @ 70kg"


async def test_a_different_day_is_a_different_session(root: Path) -> None:
    _, state, _ = await run_cap(
        root, {"action": "record", "what": "Back Squat", "how_much": "5x5 @ 80kg", "when": "2026-10-18"}
    )
    _, state2, _ = await run_cap(
        root, {"action": "record", "what": "Back Squat", "how_much": "5x5 @ 85kg", "when": "2026-10-20"}, state=state
    )
    assert len(state2["sessions"]) == 2, "a session a day — the log grows by the calendar, not by retries"


async def test_a_future_session_is_refused(root: Path) -> None:
    future = (date.today() + timedelta(days=2)).isoformat()
    result, state, _ = await run_cap(
        root, {"action": "record", "what": "Back Squat", "how_much": "5x5", "when": future}
    )
    assert not result["ok"] and "not promised" in result["error"] and state == {}


# -- reporting: computed from the stored rows, or nothing ------------------------


async def test_the_report_is_computed_from_stored_sessions(root: Path) -> None:
    seeded = {
        "sessions": {
            "back-squat::2026-10-14": {"what": "Back Squat", "how_much": "5x5 @ 75kg", "day": "2026-10-14"},
            "back-squat::2026-10-18": {"what": "Back Squat", "how_much": "5x5 @ 80kg", "day": "2026-10-18"},
            "press::2026-10-18": {"what": "Press", "how_much": "5x3 @ 45kg", "day": "2026-10-18"},
        }
    }
    result, _state, notifications = await run_cap(root, {"action": "report"}, state=seeded)

    assert result["ok"] and result["total_sessions"] == 3
    squat = next(r for r in result["exercises"] if r["what"] == "Back Squat")
    assert squat["sessions"] == 2 and squat["last_how_much"] == "5x5 @ 80kg" and squat["last_day"] == "2026-10-18"
    assert notifications and "stored sessions only" in notifications[0]["text"], (
        "the digest names its own source — a record, not a recollection"
    )


async def test_an_empty_record_reports_ok_and_does_not_nag(root: Path) -> None:
    result, _state, notifications = await run_cap(root, {"action": "report"})
    assert result["ok"] and result["total_sessions"] == 0
    assert notifications == [], "nothing logged is nothing to say"
