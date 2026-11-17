"""The self-test gate: it verifies the machine, and it must not dirty what it verifies."""

from __future__ import annotations

from pathlib import Path

from daedalus.config import Settings
from daedalus.selftest import run_selftest


async def test_the_self_test_passes_and_a_broken_layout_fails_it(settings: Settings) -> None:
    report = await run_selftest(settings)
    assert report.ok, report.render()
    names = [c.name for c in report.checks]
    assert "layout" in names and "capability gate" in names and "loop" in names

    # The hardening stage must catch a home that is not locked down.
    from daedalus.storage import paths

    home = settings.home.parent / "loose"
    paths.ensure_tree(home)
    home.chmod(0o755)
    broken = await run_selftest(Settings(home=home, env={}))
    layout = next(c for c in broken.checks if c.name == "layout")
    assert not layout.ok
    assert not broken.ok


async def test_the_self_test_leaves_the_home_exactly_as_it_found_it(settings: Settings) -> None:
    """A verification gate that dirties the thing it verifies is not a gate."""
    from daedalus.memory.store import MemoryStore
    from daedalus.scheduler.scheduler import Scheduler

    before = sorted(str(p) for p in settings.home.rglob("*"))

    report = await run_selftest(settings)
    assert report.ok, report.render()

    after = sorted(str(p) for p in settings.home.rglob("*"))
    assert after == before, f"self-test added: {sorted(set(after) - set(before))}"
    assert list((settings.home / "capabilities").iterdir()) == [], "a capability was registered"
    assert Scheduler(settings.home / "state").jobs() == [], "a job was scheduled"
    assert MemoryStore(settings.home / "memory").count() == 0, "a memory was written"


def test_the_system_checks_read_the_machines_last_word_and_name_the_fix(
    monkeypatch, tmp_path: Path  # noqa: ANN001
) -> None:
    import textwrap

    import daedalus.selftest as st

    # Vendor default, /etc override, drop-in — last assignment wins; a comment is not the machine's word.
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

    assert st._effective_logind(key="HandleLidSwitch", default="suspend", files=[vendor]) == "lock", (
        "an uncommented vendor value is the machine's word; a comment is not"
    )
    assert st._effective_logind(key="HandleLidSwitch", default="suspend", files=[vendor, etc]) == "suspend"
    assert (
        st._effective_logind(key="HandleLidSwitch", default="suspend", files=[vendor, etc, drop_in])
        == "ignore"
    )

    # A suspending lid is a failure with its fix in the sentence, not a wiki elsewhere.
    settings = Settings(home=Path.home(), env={})
    failing = st._check_lid(settings, value="suspend")
    assert not failing.ok and "HandleLidSwitch=ignore" in failing.detail, (
        "the fix's actual lines belong in the failure"
    )
    passing = st._check_lid(settings, value="ignore")
    assert passing.ok and passing.detail == "HandleLidSwitch=ignore"
    odd = st._check_lid(settings, value="lock")
    assert odd.ok and "awake" in odd.detail, "lock keeps the machine awake; it is not a suspend"

    # The funnel check names what it read, in all three readings.
    monkeypatch.setattr(st, "funnel_on", lambda: True)
    on = st._check_funnel(settings)
    assert not on.ok and "Funnel is ON" in on.detail

    monkeypatch.setattr(st, "funnel_on", lambda: False)
    off = st._check_funnel(settings)
    assert off.ok and "only way in" in off.detail

    monkeypatch.setattr(st, "funnel_on", lambda: None)
    unknown = st._check_funnel(settings)
    assert not unknown.ok and "unknown" in unknown.detail
