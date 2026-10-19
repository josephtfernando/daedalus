"""The habit tracker (T36): marks, misses, and the streak the stored days imply.

Through the real runner (subprocess, protocol §9.3), exactly as a scheduled
run or a turn's `capability op=run` will invoke it. The *seat* is deliberately
absent from these tests: whose wake runs this is the owner's decision
(recorded in TICKETS.md), and the capability's state is seat-agnostic truth —
that is the implementable part, complete.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from daedalus.capabilities.manifest import Manifest
from daedalus.capabilities.runner import CapabilityRunner


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    source = Path.home() / ".daedalus" / "capabilities" / "habit_tracker"
    dest = tmp_path / "habit_tracker"
    dest.mkdir(parents=True)
    (dest / "run.py").write_text((source / "run.py").read_text(encoding="utf-8"), encoding="utf-8")
    manifest = Manifest.from_path(source / "manifest.toml")
    manifest.save(dest)
    return tmp_path


async def run_cap(root: Path, args: dict, state: dict | None = None) -> tuple[dict, dict, list]:
    manifest = Manifest.from_path(root / "habit_tracker" / "manifest.toml")
    out = await CapabilityRunner(root, use_venv=False).run(manifest, args=args, state=state)
    assert out.ok, out.error or out.stdout
    return out.result, out.state_next, out.notifications


SEED = {
    "habits": {
        "sleep-by-midnight": {
            "habit": "sleep by midnight",
            "days": {
                "2026-10-12": "done",
                "2026-10-13": "done",
                "2026-10-14": "done",
                "2026-10-15": "done",
                "2026-10-16": "missed",
                "2026-10-17": "done",
                "2026-10-18": "done",
            },
        }
    }
}


async def test_record_marks_a_day(root: Path) -> None:
    result, state, _ = await run_cap(
        root, {"action": "record", "habit": "evening stretch", "mark": "done", "date": "2026-10-16"}
    )
    assert result["ok"] and not result["warnings"]
    (row,) = state["habits"].values()
    assert row["days"]["2026-10-16"] == "done"


async def test_a_miss_is_recorded_and_visible(root: Path) -> None:
    """A missed day is a mark the report shows — never a silent absence."""
    result, state, _ = await run_cap(
        root, {"action": "record", "habit": "evening stretch", "mark": "missed", "date": "2026-10-16"}
    )
    assert result["ok"], result
    report, _state, _ = await run_cap(root, {"action": "report"}, state=state)
    (row,) = report["habits"]
    marks = {d["day"]: d["mark"] for d in row["last7"]}
    assert marks["2026-10-16"] == "missed", "the miss is in the grid, named"
    assert any(m["date"] == "2026-10-16" for m in row["misses"]), "and in the misses list"


async def test_the_streak_is_what_the_stored_days_imply(root: Path) -> None:
    """5 done (12–16 pattern minus the miss), miss on 16, done 17–18 → current 2, longest 4.

    The stored days here are: 12,13,14,15 done; 16 missed; 17,18 done. The
    current streak walks back from the newest `done` and stops at the stored
    miss; an UNRECORDED day stops it exactly the same way.
    """
    report, _state, _ = await run_cap(root, {"action": "report"}, state=SEED)
    (row,) = report["habits"]
    assert row["streak"] == 2, "17–18 are done; the stored miss on 16 stopped the walk"
    assert row["longest_streak"] == 4, "12–15 is the best complete run in the record"
    assert row["marks"] == 6 and len(row["misses"]) == 1


async def test_an_unrecorded_day_breaks_the_streak(root: Path) -> None:
    """Absent ≠ done: honesty beats flattery."""
    seed = {
        "habits": {
            "morning-pages": {
                "habit": "morning pages",
                "days": {"2026-10-13": "done", "2026-10-14": "done", "2026-10-18": "done"},
            }
        }
    }
    report, _state, _ = await run_cap(root, {"action": "report"}, state=seed)
    (row,) = report["habits"]
    assert row["streak"] == 1, "14→18 has three unrecorded days; the streak is the stored days' word"


async def test_the_same_day_is_an_update_not_a_second_mark(root: Path) -> None:
    result, state, _ = await run_cap(
        root, {"action": "record", "habit": "evening stretch", "mark": "done", "date": "2026-10-15"}
    )
    assert result["ok"]
    again, state2, _ = await run_cap(
        root, {"action": "record", "habit": "evening stretch", "mark": "missed", "date": "2026-10-15"}, state=state
    )
    assert again["ok"] and any("was already marked" in w for w in again["warnings"]), (
        "the correction says what it corrected"
    )
    (row,) = state2["habits"].values()
    assert list(row["days"].items()) == [("2026-10-15", "missed")], (
        "one day, one mark — the correction stands, no ghost of the old mark"
    )


async def test_how_am_i_doing_comes_from_state(root: Path) -> None:
    """The report is the stored record, not a memory — a notification names its source."""
    _result, _state, notifications = await run_cap(root, {"action": "report"}, state=SEED)
    assert notifications, "a report of a lived habit speaks"
    text = notifications[0]["text"]
    assert "stored days" in text, "the digest says where its answers came from"
    assert "sleep by midnight" in text and "streak" in text


async def test_marks_are_validated_before_stored(root: Path) -> None:
    result, state, _ = await run_cap(root, {"action": "record", "habit": "x", "mark": "kind-of", "date": "2026-10-16"})
    assert not result["ok"] and "done" in result["error"] and state == {}
    future = (date.today() + timedelta(days=2)).isoformat()
    result, _state, _ = await run_cap(
        root, {"action": "record", "habit": "x", "mark": "done", "date": future}
    )
    assert not result["ok"] and "in the future" in result["error"], "a habit is marked, not promised"
