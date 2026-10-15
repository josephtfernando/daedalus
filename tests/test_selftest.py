from __future__ import annotations

from daedalus.config import Settings
from daedalus.selftest import run_selftest


async def test_selftest_passes(settings: Settings) -> None:
    report = await run_selftest(settings)
    assert report.ok, report.render()
    names = [c.name for c in report.checks]
    assert "layout" in names and "capability gate" in names and "loop" in names


async def test_selftest_leaves_no_trace_in_the_real_home(settings: Settings) -> None:
    """A verification gate that dirties the thing it verifies is not a gate."""
    before = sorted(str(p) for p in settings.home.rglob("*"))

    report = await run_selftest(settings)
    assert report.ok, report.render()

    after = sorted(str(p) for p in settings.home.rglob("*"))
    assert after == before, f"self-test added: {sorted(set(after) - set(before))}"


async def test_selftest_does_not_register_capabilities(settings: Settings) -> None:
    await run_selftest(settings)
    assert list((settings.home / "capabilities").iterdir()) == []


async def test_selftest_does_not_schedule_anything(settings: Settings) -> None:
    await run_selftest(settings)
    from daedalus.scheduler.scheduler import Scheduler

    assert Scheduler(settings.home / "state").jobs() == []


async def test_selftest_does_not_write_memory(settings: Settings) -> None:
    await run_selftest(settings)
    from daedalus.memory.store import MemoryStore

    assert MemoryStore(settings.home / "memory").count() == 0


async def test_selftest_reports_failure_on_a_broken_layout(tmp_path) -> None:
    from daedalus.storage import paths

    home = tmp_path / "loose"
    paths.ensure_tree(home)
    home.chmod(0o755)  # the hardening stage must catch this

    report = await run_selftest(Settings(home=home, env={}))
    layout = next(c for c in report.checks if c.name == "layout")
    assert not layout.ok
    assert not report.ok
