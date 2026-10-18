"""The settings tool (T35, carried from T34): the roots list is owner-owned data.

Two halves are held. The tool's own half: `roots-add`/`roots-remove` are ASKs
because reach growing is an approval, the change lands in `roots.toml` (the
owner's file, at 0600) *and* is true for reads immediately — the guard reads
the live dict, so "took effect without a restart" and "went through an
approval" are true in the same breath. The page's half: the dashboard's
editor reaches the same ops through the control seam, where
"waiting" means the buttons are on Discord, and a read is not a control the
page may propose — the page already shows the list.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

import pytest

from daedalus.approvals.classify import Level
from daedalus.approvals.risk import Risk
from daedalus.runtime import Runtime
from daedalus.tools.settings import SettingsTool, _write_roots

# -- the tool, on its own path ------------------------------------------------


def _roots_toml(runtime: Runtime) -> Path:
    return runtime.settings.home / "roots.toml"


async def test_roots_list_names_what_is_declared(runtime: Runtime) -> None:
    runtime.settings.raw["files"] = {"roots": [str(runtime.settings.home / "vault")]}
    result = await SettingsTool().run({"op": "roots-list"}, runtime)

    assert result.ok
    assert str(runtime.settings.home / "vault") in result.output


async def test_roots_add_persists_and_is_true_now(runtime: Runtime) -> None:
    """One op, two truths at once: the file holds it and the guard reads it."""
    runtime.settings.raw["files"] = {"roots": []}
    target = runtime.settings.home / "served"
    target.mkdir()

    result = await SettingsTool().run({"op": "roots-add", "path": str(target)}, runtime)

    assert result.ok
    body = _roots_toml(runtime).read_text(encoding="utf-8")
    assert "served" in body
    assert runtime.settings.raw["files"]["roots"] == [str(target)], (
        "the guard reads the live dict — if the change only sat in the file, "
        "it would stand only after the next restart"
    )
    assert _roots_toml(runtime).stat().st_mode & 0o077 == 0, (
        "the root list is an access-control table; it is not world-readable"
    )


async def test_an_already_declared_root_changes_nothing(runtime: Runtime) -> None:
    existing = runtime.settings.home / "already"
    existing.mkdir()
    _write_roots(runtime.settings.home, [str(existing)])
    runtime.settings.raw["files"] = {"roots": [str(existing)]}
    before = _roots_toml(runtime).read_text(encoding="utf-8")

    result = await SettingsTool().run({"op": "roots-add", "path": str(existing)}, runtime)

    assert result.ok, result.output
    assert _roots_toml(runtime).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("op", "path", "frag"),
    [
        ("roots-add", "relative/place", "absolute"),
        ("roots-add", "", "`path`"),
        ("roots-remove", str(Path("/nowhere/on/this/disk")), "not in the declared roots"),
        ("roots-add", str(Path("/nowhere/on/this/disk")), "not a directory"),
        ("roots-burn", "/x", "unknown settings op"),
    ],
)
async def test_a_root_change_says_why_not_when_it_cannot(
    runtime: Runtime, op: str, path: str, frag: str
) -> None:
    runtime.settings.raw["files"] = {"roots": []}
    result = await SettingsTool().run({"op": op, "path": path}, runtime)

    assert not result.ok
    assert frag in result.output


async def test_roots_remove_ends_access_without_deleting_anything(runtime: Runtime) -> None:
    gone = runtime.settings.home / "ousted"
    gone.mkdir()
    runtime.settings.raw["files"] = {"roots": [str(gone)]}

    result = await SettingsTool().run({"op": "roots-remove", "path": str(gone)}, runtime)

    assert result.ok and result.output.startswith("removed")
    assert runtime.settings.raw["files"]["roots"] == []
    assert gone.is_dir(), "removing a root lets files go out of reach; it does not delete them"


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


def test_an_add_from_the_page_waits_then_stands_deny_then_allow(
    runtime: Runtime,
) -> None:
    """The page proposes; a *no* leaves nothing standing, a *yes* makes it stand."""
    from daedalus.genui.actions import Controls

    surface = Recorder()
    surface.answer = False
    runtime.set_surface(surface)  # type: ignore[arg-type]
    runtime.settings.raw["files"] = {"roots": []}
    target = runtime.settings.home / "served"
    target.mkdir()

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

    surface.answer = True
    with Loop() as loop:
        Controls(runtime, loop=loop).perform("settings.roots-add", {"path": str(target)})
        _settle(lambda: bool(surface.requests) and _roots_toml(runtime).exists())

    assert runtime.settings.raw["files"]["roots"] == [str(target)], (
        "approved, so it reads live — and roots.toml holds it for the next startup"
    )


def test_a_read_is_not_a_control(runtime: Runtime) -> None:
    from daedalus.genui.actions import Controls

    outcome = Controls(runtime).perform("settings.roots-list", {})

    assert outcome.status == "refused" and outcome.code == 400
    assert "not a control" in outcome.message, (
        "the page already shows the list; a control is only ever a write"
    )
