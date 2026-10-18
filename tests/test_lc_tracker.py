"""The LC tracker (T37): practice as coverage and staleness, and the row is the record.

The capability runs as a real subprocess through the runner (protocol §9.3),
because a tracker whose in/out shape is only ever seen through Python objects
has not proven the contract it will run under twice a day.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.capabilities.manifest import Manifest
from daedalus.capabilities.runner import CapabilityRunner


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    source = Path.home() / ".daedalus" / "capabilities" / "lc_tracker"
    dest = tmp_path / "lc_tracker"
    dest.mkdir(parents=True)
    (dest / "run.py").write_text((source / "run.py").read_text(encoding="utf-8"), encoding="utf-8")
    manifest = Manifest.from_path(source / "manifest.toml")
    manifest.save(dest)
    return tmp_path


async def run_cap(root: Path, args: dict, state: dict | None = None) -> tuple[dict, dict, list]:
    manifest = Manifest.from_path(root / "lc_tracker" / "manifest.toml")
    out = await CapabilityRunner(root, use_venv=False).run(manifest, args=args, state=state)
    assert out.ok, out.error or out.stdout
    return out.result, out.state_next, out.notifications


async def test_the_same_problem_updates_its_row(root: Path) -> None:
    """'recorded twice' is an update, not a second row — a tracker, not a log."""
    first, state, _ = await run_cap(
        root,
        {"action": "record", "problem": "122. Best Time to Buy and Sell Stock II", "topic": "dp", "result": "failed"},
    )
    assert first["ok"] and first["recorded"] == 1
    second, state, _ = await run_cap(
        root,
        {"action": "record", "problem": "122. Best Time to Buy and Sell Stock II", "topic": "dp", "result": "solved"},
        state=state,
    )
    assert second["ok"] and second["recorded"] == 1, (
        f"recording the same problem again must stay one row (got {second['recorded']})"
    )
    rows = state["rows"]
    (row,) = rows.values()
    assert [h["result"] for h in row["history"]] == ["failed", "solved"], (
        "the update keeps the attempt history — 'how many times did I fall over this' survives"
    )


async def test_a_topic_move_is_visible_not_silent(root: Path) -> None:
    """An update under a different topic is a warning the record carries."""
    _, state, _ = await run_cap(
        root, {"action": "record", "problem": "Valid Parentheses", "topic": "stack", "result": "solved"}
    )
    result, _state, _ = await run_cap(
        root,
        {"action": "record", "problem": "Valid Parentheses", "topic": "strings", "result": "reviewed"},
        state=state,
    )
    assert result["warnings"], "the row moved topics; the record says so in the same breath"
    assert "stack" in result["warnings"][0] and "strings" in result["warnings"][0]


async def test_report_names_what_has_gone_stale(root: Path) -> None:
    """Coverage decay is a computed fact of the record, not a recollection."""
    _result, state, _ = await run_cap(
        root,
        {"action": "record", "problem": "Two Sum", "topic": "hashmap", "result": "solved"},
        state={"rows": {"warm-up": {"problem": "warm-up", "topic": "dp", "last": "2026-09-20", "history": []}}},
    )
    result, _state, notifications = await run_cap(root, {"action": "report"}, state=state)
    assert result["ok"] and result["total_problems"] == 2
    stale = set(result["stale_topics"])
    assert "dp" in stale, "a topic cold since 20 Sep is stale — that is the point of the tracker"
    assert "hashmap" not in stale, "today's touch is warm"
    assert notifications, "the cadence act reports; silence is 'nothing recorded', not 'all fine'"
    assert "due for a repeat" in notifications[0]["text"] and "STALE" in notifications[0]["text"]


async def test_a_report_of_an_empty_tracker_is_quiet(root: Path) -> None:
    result, _state, notifications = await run_cap(root, {"action": "report"})
    assert result["ok"] and result["total_problems"] == 0
    assert notifications == [], "nothing recorded is nothing to nag about"


async def test_a_record_refuses_what_it_cannot_count(root: Path) -> None:
    result, _state, _ = await run_cap(
        root, {"action": "record", "problem": "Two Sum", "topic": "vibes", "result": "solved"}
    )
    assert not result["ok"] and "not one of the tracker's topics" in result["error"], (
        "an unlisted topic would silently never appear in a report — refused, not stored"
    )
    result, _state, _ = await run_cap(
        root, {"action": "record", "problem": "Two Sum", "topic": "dp", "result": "crushed it"}
    )
    assert not result["ok"] and "result" in result["error"]
