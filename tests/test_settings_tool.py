"""The settings tool (T35, carried from T34): the roots list is owner-owned data.

Two halves are held. The tool's half: `roots-add`/`roots-remove` are ASKs because
reach growing is an approval, and the change lands in `roots.toml` (the owner's
file, at 0600) *and* is true for reads immediately. The page's half: the
dashboard's editor reaches the same ops through the control seam, where
"waiting" means the buttons are on Discord, and a read is not a control the page
may propose.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daedalus.approvals.classify import Level
from daedalus.approvals.risk import Risk
from daedalus.runtime import Runtime
from daedalus.tools.settings import SettingsTool


def _roots_toml(runtime: Runtime) -> Path:
    return runtime.settings.home / "roots.toml"


async def test_adding_a_root_persists_it_at_0600_and_is_true_for_reads_at_once(
    runtime: Runtime,
) -> None:
    runtime.settings.raw["files"] = {"roots": []}
    target = runtime.settings.home / "served"
    target.mkdir()

    result = await SettingsTool().run({"op": "roots-add", "path": str(target)}, runtime)

    # one op, two truths: the file holds it and the guard reads it
    assert result.ok
    assert "served" in _roots_toml(runtime).read_text(encoding="utf-8")
    assert runtime.settings.raw["files"]["roots"] == [str(target)], (
        "the guard reads the live dict — if the change only sat in the file, "
        "it would stand only after the next restart"
    )
    assert _roots_toml(runtime).stat().st_mode & 0o077 == 0, (
        "the root list is an access-control table; it is not world-readable"
    )

    # an already-declared root changes nothing
    before = _roots_toml(runtime).read_text(encoding="utf-8")
    again = await SettingsTool().run({"op": "roots-add", "path": str(target)}, runtime)
    assert again.ok, again.output
    assert _roots_toml(runtime).read_text(encoding="utf-8") == before

    # the listing names what is declared
    listed = await SettingsTool().run({"op": "roots-list"}, runtime)
    assert listed.ok
    assert str(target) in listed.output

    # removal ends access without deleting anything
    removed = await SettingsTool().run({"op": "roots-remove", "path": str(target)}, runtime)
    assert removed.ok and removed.output.startswith("removed")
    assert runtime.settings.raw["files"]["roots"] == []
    assert target.is_dir(), "removing a root lets files go out of reach; it does not delete them"


async def test_a_root_change_says_why_not_when_it_cannot(runtime: Runtime) -> None:
    runtime.settings.raw["files"] = {"roots": []}
    cases = [
        ("roots-add", "relative/place", "absolute"),
        ("roots-add", "", "`path`"),
        ("roots-remove", "/nowhere/on/this/disk", "not in the declared roots"),
        ("roots-add", "/nowhere/on/this/disk", "not a directory"),
        ("roots-burn", "/x", "unknown settings op"),
    ]
    for op, path, frag in cases:
        result = await SettingsTool().run({"op": op, "path": path}, runtime)
        assert not result.ok, f"{op} {path!r} was accepted"
        assert frag in result.output, f"{op} {path!r}: {result.output}"


def test_the_writes_are_asks_and_the_read_is_auto(runtime: Runtime) -> None:
    tool = SettingsTool()
    assert tool.risk_for({"op": "roots-add", "path": "/x"}) is Risk.ASK
    assert tool.risk_for({"op": "roots-remove", "path": "/x"}) is Risk.ASK
    assert tool.risk_for({"op": "roots-list"}) is Risk.AUTO
    assert tool.assess({"op": "roots-add", "path": "/x"}).level is Level.CAUTION
    assert "widen" in tool.assess({"op": "roots-add", "path": "/x"}).summary


# -- the page's path: the control seam ---------------------------------------


class Recorder:
    """A surface that records what was asked, and answers on demand."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.sent: list[str] = []
        self.answer = True

    async def send(self, text: str, *, thread: str | None = None) -> None:
        self.sent.append(text)

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        return ""

    async def approve(self, request: Any) -> bool:
        self.requests.append(request)
        return self.answer


@dataclass
class Loop:
    def __enter__(self) -> Any:
        self.loop = __import__("asyncio").new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _settle(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("the daemon never caught up")


def test_the_page_can_only_propose_a_write_and_it_waits_for_the_owner(
    runtime: Runtime,
) -> None:
    from daedalus.genui.actions import Controls

    surface = Recorder()
    surface.answer = False
    runtime.set_surface(surface)  # type: ignore[arg-type]
    runtime.settings.raw["files"] = {"roots": []}
    target = runtime.settings.home / "served"
    target.mkdir()

    # a no is nothing standing
    with Loop() as loop:
        outcome = Controls(runtime, loop=loop).perform(
            "settings.roots-add", {"path": str(target)}
        )
        assert outcome.status == "waiting" and outcome.code == 202, (
            "widening reach is never a click-only act — the request went to the owner"
        )
        _settle(lambda: bool(surface.sent))

    request = surface.requests[0]
    assert request.action == "settings"
    assert request.args["op"] == "roots-add"
    assert not _roots_toml(runtime).exists(), "a no is nothing standing"
    assert runtime.settings.raw["files"]["roots"] == []

    # a yes makes it stand, live and in the file
    surface.answer = True
    with Loop() as loop:
        Controls(runtime, loop=loop).perform("settings.roots-add", {"path": str(target)})
        _settle(lambda: bool(surface.requests) and _roots_toml(runtime).exists())

    assert runtime.settings.raw["files"]["roots"] == [str(target)], (
        "approved, so it reads live — and roots.toml holds it for the next startup"
    )

    # a read is not a control the page may propose; the page already shows it
    read = Controls(runtime).perform("settings.roots-list", {})
    assert read.status == "refused" and read.code == 400
    assert "not a control" in read.message
