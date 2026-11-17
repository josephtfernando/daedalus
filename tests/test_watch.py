"""Watches: a path changing is a trigger no clock can express.

`schedule` is for what recurs and `remind` for a named moment. A watch is the
third kind — *that* happened — and it comes with three traps these tests hold
shut: a watch that fires the moment it is created (the baseline is taken before
it exists), a watch that fires on every tick because the file is being written
(the cooldown), and a watch that never stops (it is a one-shot by default).

The other property is what makes watches different from wakes: the owner asked
for this, so the proactive gate has no say and a silent model does not get to
swallow the fact.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from daedalus.runtime import Runtime
from daedalus.scheduler.watches import (
    MAX_WATCHES,
    MIN_COOLDOWN_S,
    Watch,
    WatchStore,
    any_fire_due,
    describe_fire,
    prompt_for,
    signature,
)

NOON = datetime(2026, 10, 16, 12, 0)


def test_a_watch_is_private_state_that_never_fires_on_what_was_already_true(tmp_path: Path) -> None:
    # The signature: absent is absent, not unknown; present is (mtime, size).
    assert signature(tmp_path / "nope") is None
    present = tmp_path / "there.txt"
    present.write_text("x")
    assert signature(present) == [present.stat().st_mtime_ns, 1]

    # A watch survives a round trip through the store, one-shot by default.
    path = tmp_path / "build.log"
    path.write_text("done")
    store = WatchStore(tmp_path / "state" / "watches.json")
    added = store.add(path=str(path), note="the build finished", cooldown_s=120)
    (back,) = store.all()
    assert back.id == added.id
    assert back.path == str(path)
    assert back.note == "the build finished"
    assert back.once is True
    assert back.cooldown_s == 120
    assert back.signature == signature(path)

    # The watch file is owner-only.
    assert store.path.stat().st_mode & 0o777 == 0o600

    # The baseline is taken at add, for a file that already exists...
    old = tmp_path / "old.log"
    old.write_text("written an hour ago")
    baseline = WatchStore(tmp_path / "baseline" / "watches.json")
    baseline.add(path=str(old), note="it changed")
    assert any_fire_due(baseline.all(), now=1e12) == []

    # ...and for a path that is not there yet it is baselined as absent.
    later = baseline.add(path=str(tmp_path / "later.txt"), note="it landed")
    assert later.signature is None
    assert any_fire_due(baseline.all(), now=1e12) == []


def test_a_damaged_or_full_store_is_refused_before_it_lies(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state" / "watches.json")

    # A corrupt file is no watches, not a crash.
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json")
    assert store.all() == []

    # The number of watches is bounded.
    for index in range(MAX_WATCHES):
        store.add(path=str(tmp_path / f"{index}.txt"), note="x")
    with pytest.raises(ValueError, match="already"):
        store.add(path=str(tmp_path / "one-too-many.txt"), note="x")

    # An unknown mode is refused.
    other = WatchStore(tmp_path / "other" / "watches.json")
    with pytest.raises(ValueError, match="change or missing"):
        other.add(path=str(tmp_path / "x"), note="x", on="whatever")

    # Removing reports whether the watch existed.
    watch = other.add(path=str(tmp_path / "x"), note="x")
    assert other.remove(watch.id) is True
    assert other.remove(watch.id) is False
    assert other.all() == []


def test_a_change_is_judged_by_mode_and_absence_is_never_silence() -> None:
    # Change mode: a different mtime or size is a change; the same is nothing.
    watch = Watch(id="w1", path="/x", note="n", signature=[1, 10])
    assert watch.trigger([2, 10]) == "changed"
    assert watch.trigger([2, 11]) == "changed"
    assert watch.trigger([1, 10]) == ""

    # A file that was not there appears.
    assert Watch(id="w1", path="/x", note="n", signature=None).trigger([1, 1]) == "appeared"

    # A file that goes away disappears even in change mode: silence is the wrong
    # answer when the thing stopped existing.
    assert watch.trigger(None) == "disappeared"

    # Missing mode waits for the disappearance and nothing else...
    missing = Watch(id="w1", path="/x", note="n", on="missing", signature=[1, 10])
    assert missing.trigger([2, 20]) == "", "a change is not what was asked for"
    assert missing.trigger(None) == "disappeared"

    # ...and a path that was already absent never fires.
    assert Watch(id="w1", path="/x", note="n", on="missing", signature=None).trigger(None) == ""


def test_a_cooldown_holds_however_often_the_file_changes(tmp_path: Path) -> None:
    # A cooldown below the floor is raised to it.
    store = WatchStore(tmp_path / "state" / "watches.json")
    floored = store.add(path=str(tmp_path / "x"), note="x", once=False, cooldown_s=1)
    assert floored.cooldown_s == MIN_COOLDOWN_S

    # A changing file does not become a metronome.
    path = tmp_path / "growing.log"
    path.write_text("one")
    watch = store.add(path=str(path), note="the log grew", once=False, cooldown_s=600)
    assert watch.may_fire(1_000.0) is True, "nothing has fired yet, so it may"
    watch.fired_at = 1_000.0
    assert watch.may_fire(1_000.0 + 599) is False
    assert watch.may_fire(1_000.0 + 600) is True

    # A change inside the cooldown is skipped; delivery moves the signature and
    # the moment together, and after the cooldown it fires again.
    path.write_text("one and two")
    now = 1_000.0
    due = any_fire_due(store.all(), now)
    assert [what for _w, what, _s in due if _w.path == str(path)] == ["changed"]
    watch = next(w for w in store.all() if w.path == str(path))
    watch.fired_at = now
    watch.signature = signature(path)
    store.put(watch)
    path.write_text("one and two and three")
    assert [d for d in any_fire_due(store.all(), now + 60) if d[0].path == str(path)] == [], (
        "inside the cooldown"
    )
    assert [what for w, what, _s in any_fire_due(store.all(), now + 600) if w.path == str(path)] == [
        "changed"
    ]

    # The same guard end to end, with real stat calls: a file written every
    # second inside the floor fires none of thirty times.
    e2e_path = tmp_path / "sync" / "file.bin"
    e2e_path.parent.mkdir()
    e2e_path.write_text("0")
    e2e_store = WatchStore(tmp_path / "e2e" / "watches.json")
    e2e_store.add(path=str(e2e_path), note="sync moved", once=False, cooldown_s=MIN_COOLDOWN_S)

    fired = 0
    moment = 1_000.0
    for _tick in range(10):
        moment += 60
        e2e_path.write_text(f"written at {moment}")
        os.utime(e2e_path, ns=(int(moment * 1e9), int(moment * 1e9)))
        due = any_fire_due(e2e_store.all(), moment)
        if not due:
            continue
        fired += 1
        touched = e2e_store.all()[0]
        touched.fired_at = moment
        touched.signature = signature(e2e_path)
        e2e_store.put(touched)

    assert fired == 10, "a minute apart is outside a 30s cooldown — each is a real change"

    touched = e2e_store.all()[0]
    for _ in range(30):
        e2e_path.write_text("x" * (touched.signature[1] + 1 if touched.signature else 1))
        os.utime(e2e_path, ns=(int(moment * 1e9), int(moment * 1e9)))
        assert any_fire_due(e2e_store.all(), moment) == []
        moment += 1


# -- firing ----------------------------------------------------------------


def _watch_on(tmp_path: Path, name: str = "out.txt", **kwargs) -> tuple[WatchStore, Path]:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("first")
    store = WatchStore(tmp_path / "state" / "watches.json")
    store.add(path=str(path), note="the export landed", **kwargs)
    return store, path


async def test_a_fire_is_delivered_and_a_one_shot_is_removed(runtime: Runtime, tmp_path: Path) -> None:
    # A change is delivered, and the one-shot is gone after it speaks.
    store, path = _watch_on(tmp_path)
    runtime.scheduler.watches = store
    path.write_text("second")
    results = await runtime.scheduler.fire_watches(runtime, None, NOON)
    assert [r.ok for r in results] == [True]
    assert "the export landed" in runtime.surface.sent[0]
    assert "out.txt" in runtime.surface.sent[0]
    assert store.all() == [], "a one-shot is gone after it speaks"

    # A repeating watch stays and records when it spoke.
    repeat_store, repeat_path = _watch_on(tmp_path / "repeat", once=False)
    runtime.scheduler.watches = repeat_store
    repeat_path.write_text("second")
    await runtime.scheduler.fire_watches(runtime, None, NOON)
    (watch,) = repeat_store.all()
    assert watch.fired_at == NOON.timestamp()
    assert watch.signature == signature(repeat_path)

    # Watches are a scheduler step, not a second scheduler.
    tick_store, tick_path = _watch_on(tmp_path / "tick")
    runtime.scheduler.watches = tick_store
    tick_path.write_text("second")
    ticked = await runtime.scheduler.dispatch_tick(runtime, when=NOON)
    assert any(r.job.startswith("watch:") for r in ticked)
    assert runtime.surface.sent

    # A failed delivery is retried on the next tick: the signature moves only
    # after the message is actually sent.
    fail_store, fail_path = _watch_on(tmp_path / "fail")
    runtime.scheduler.watches = fail_store
    fail_path.write_text("second")

    class Broken:
        def __init__(self) -> None:
            self.tries = 0

        async def send(self, text: str, *, thread: str | None = None) -> None:
            self.tries += 1
            raise OSError("the surface is down")

    broken = Broken()
    runtime.surface = broken  # type: ignore[assignment]

    results = await runtime.scheduler.fire_watches(runtime, None, NOON)
    assert [r.ok for r in results] == [False]
    assert len(fail_store.all()) == 1, "the watch must survive a failed delivery"

    await runtime.scheduler.fire_watches(runtime, None, NOON)
    assert len(fail_store.all()) == 1
    assert broken.tries == 2


async def test_the_model_may_phrase_a_fire_but_never_swallow_or_invent_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    # A silent model does not swallow the fact: a watch is the owner's
    # instruction, and the plain line still says when.
    silent_store, silent_path = _watch_on(tmp_path / "silent")
    runtime.scheduler.watches = silent_store
    silent_path.write_text("second")

    async def silent(_prompt: str) -> str:
        return "   "

    await runtime.scheduler.fire_watches(runtime, silent, NOON)
    assert runtime.surface.sent and "the export landed" in runtime.surface.sent[0]
    assert "12:00" in runtime.surface.sent[0], "the plain line still says when"

    # The model may phrase it: it is given the change to explain, and its words are the message.
    compose_store, compose_path = _watch_on(tmp_path / "compose")
    runtime.scheduler.watches = compose_store
    compose_path.write_text("second")
    asked: list[str] = []

    async def compose(prompt: str) -> str:
        asked.append(prompt)
        return "The export finished — 412 rows, no failures."

    runtime.surface.sent.clear()
    await runtime.scheduler.fire_watches(runtime, compose, NOON)
    assert runtime.surface.sent == ["The export finished — 412 rows, no failures."]
    assert "the export landed" in asked[0]
    assert "changed" in asked[0]

    # The plain line names the note, the file and the time...
    watch = Watch(id="w1", path="/tmp/build.log", note="the build finished")
    line = describe_fire(watch, "changed", NOON.timestamp())
    assert "the build finished" in line
    assert "build.log" in line
    assert "12:00" in line

    # ...and the prompt forbids repeating the path for its own sake.
    prompt = prompt_for(watch, "disappeared")
    assert "the build finished" in prompt and "/tmp/build.log" in prompt
    assert "disappeared" in prompt
    assert "not a scheduled wake" in prompt


# -- the `watch` tool -------------------------------------------------------


async def test_the_watch_tool_adds_lists_and_removes_within_the_roots(runtime: Runtime) -> None:
    from daedalus.approvals.risk import Risk
    from daedalus.tools.watch import WatchTool

    path = runtime.settings.home / "notes" / "out.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    tool = WatchTool()

    added = await tool.run(
        {"op": "add", "path": str(path), "note": "the export landed"}, runtime
    )
    assert added.ok and added.data and added.data["id"].startswith("w")
    listed = await tool.run({"op": "list"}, runtime)
    assert listed.ok and "the export landed" in listed.output and str(path) in listed.output
    removed = await tool.run({"op": "remove", "id": added.data["id"]}, runtime)
    assert removed.ok and runtime.scheduler.watches.all() == []

    # The note is the whole message; without it a fire says nothing.
    got = await WatchTool().run({"op": "add", "path": str(runtime.settings.home)}, runtime)
    assert not got.ok and "needs `note`" in got.output

    # A path outside the roots is refused.
    got = await WatchTool().run(
        {"op": "add", "path": "/etc/passwd", "note": "someone edits it"}, runtime
    )
    assert not got.ok and "roots" in got.output

    # Watching a missing path for its disappearance would never fire — refused.
    got = await WatchTool().run(
        {
            "op": "add",
            "path": str(runtime.settings.home / "never-existed.txt"),
            "note": "it goes away",
            "on": "missing",
        },
        runtime,
    )
    assert not got.ok and "never fire" in got.output

    # ...but waiting for its arrival is allowed.
    got = await WatchTool().run(
        {"op": "add", "path": str(runtime.settings.home / "later.txt"), "note": "it landed"},
        runtime,
    )
    assert got.ok and "not there yet" in got.output

    # Removing a watch that is not there says so.
    got = await WatchTool().run({"op": "remove", "id": "wnope"}, runtime)
    assert not got.ok and "no watch" in got.output

    # Watching is local and asks nothing.
    assert tool.risk is Risk.AUTO
    assert tool.risk_for({"op": "add"}) is Risk.AUTO
    assert "no egress" in tool.assess({"op": "add", "path": "/x"}).summary

    # Models send `"false"` as often as `false`; a truthy string must not win.
    repeat_path = runtime.settings.home / "repeating.txt"
    repeat_path.write_text("x")
    got = await tool.run(
        {
            "op": "add",
            "path": str(repeat_path),
            "note": "it keeps changing",
            "once": "false",
            "cooldown_s": 120,
        },
        runtime,
    )
    assert got.ok
    (watch,) = [w for w in runtime.scheduler.watches.all() if w.path == str(repeat_path)]
    assert watch.once is False and watch.cooldown_s == 120
