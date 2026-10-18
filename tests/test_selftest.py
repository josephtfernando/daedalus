from __future__ import annotations

from pathlib import Path

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


# -- still there: the machine's own claims about itself (T27's lid half) -----


def test_effective_logind_is_the_last_files_word(tmp_path: Path) -> None:
    """Vendor default, /etc override, drop-in — last assignment wins."""
    import textwrap

    from daedalus.selftest import _effective_logind

    vendor = tmp_path / "vendor.conf"
    vendor.write_text(textwrap.dedent("""\
        [Login]
        #HandleLidSwitch=suspend
        HandleLidSwitch=lock
        """))
    etc = tmp_path / "etc.conf"
    etc.write_text("[Login]\nHandleLidSwitch=suspend\n")
    drop_in = tmp_path / "drop.conf"
    drop_in.write_text("[Login]\nHandleLidSwitch=ignore\n")

    assert _effective_logind(key="HandleLidSwitch", default="suspend", files=[vendor]) == "lock", (
        "an uncommented vendor value is the machine's word; a comment is not"
    )
    assert _effective_logind(key="HandleLidSwitch", default="suspend", files=[vendor, etc]) == "suspend"
    assert (
        _effective_logind(key="HandleLidSwitch", default="suspend", files=[vendor, etc, drop_in])
        == "ignore"
    )




def test_the_lid_check_fails_naming_the_fix_and_passes_when_fixed() -> None:
    """A suspending lid is a failure with its fix in the sentence, not a wiki elsewhere."""
    from daedalus.config import Settings
    from daedalus.selftest import _check_lid

    settings = Settings(home=Path.home(), env={})
    failing = _check_lid(settings, value="suspend")
    assert not failing.ok and "HandleLidSwitch=ignore" in failing.detail, (
        "the fix's actual lines belong in the failure"
    )

    passing = _check_lid(settings, value="ignore")
    assert passing.ok and passing.detail == "HandleLidSwitch=ignore"

    odd = _check_lid(settings, value="lock")
    assert odd.ok and "awake" in odd.detail, "lock keeps the machine awake; it is not a suspend"


def test_the_funnel_check_names_what_it_read(monkeypatch) -> None:
    import daedalus.selftest as st

    settings = Settings(home=Path.home(), env={})
    monkeypatch.setattr(st, "funnel_on", lambda: True)
    on = st._check_funnel(settings)
    assert not on.ok and "Funnel is ON" in on.detail

    monkeypatch.setattr(st, "funnel_on", lambda: False)
    off = st._check_funnel(settings)
    assert off.ok and "only way in" in off.detail

    monkeypatch.setattr(st, "funnel_on", lambda: None)
    unknown = st._check_funnel(settings)
    assert not unknown.ok and "unknown" in unknown.detail
