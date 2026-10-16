"""A scheduled capability that nobody is told about is not automation.

This is the whole point of the owner's first request: the run happens with no
model in the loop, so if the notification does not reach the surface, the
system silently does nothing and reports success. The scheduler's job is the
last hop — it has to turn what the capability printed into a message the owner
reads, and it has to do it exactly once per scheduled minute.
"""

from __future__ import annotations

from datetime import datetime

from daedalus.runtime import Runtime
from daedalus.tools.capability import CapabilityTool

FRIDAY_0730 = datetime(2026, 10, 16, 7, 30)

NOTIFY_RUN = '''"""Notifies, the way an alerting capability should."""
import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    state = payload.get("state", {}) or {}
    runs = state.get("runs", 0) + 1
    json.dump(
        {
            "result": {"ok": True, "runs": runs},
            "state_next": {"runs": runs},
            "notifications": [{"text": "Disk / is at 4.1% free."}],
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

QUIET_RUN = '''"""Does the work and says nothing — the common case."""
import json
import sys

json.dump(
    {"result": {"ok": True}, "state_next": {}, "notifications": []},
    sys.stdout,
)
'''


async def _author(runtime: Runtime, name: str, run_py: str) -> None:
    result = await CapabilityTool().run(
        {
            "op": "author",
            "name": name,
            "description": "a scheduled thing",
            "run_py": run_py,
            "schedule": "30 7 * * 1-5",
        },
        runtime,
    )
    assert result.ok, result.output
    assert result.data["verified"] is True, "it must clear the Melinoë gate before it may be scheduled"


async def test_a_scheduled_capability_reaches_the_owner(runtime: Runtime) -> None:
    await _author(runtime, "notifier", NOTIFY_RUN)

    results = await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)

    assert [r.job for r in results if r.ran] == ["cap-notifier"]
    assert runtime.surface.sent, "the notification never reached the owner"
    sent = runtime.surface.sent[-1]
    assert "Disk / is at 4.1% free." in sent, "the capability's own words were lost"
    # The owner reads the name they chose, not our internal job handle.
    assert "cap-notifier" not in sent
    assert "notifier" in sent


async def test_a_repeat_tick_does_not_message_twice(runtime: Runtime) -> None:
    """A double-fire would DM the owner twice for one scheduled minute."""
    await _author(runtime, "notifier", NOTIFY_RUN)
    await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)
    assert len(runtime.surface.sent) == 1

    again = await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)

    assert all(not r.ran for r in again), "the same minute fired twice"
    assert len(runtime.surface.sent) == 1, "the owner was messaged twice for one tick"


async def test_a_quiet_run_sends_nothing(runtime: Runtime) -> None:
    """Most runs have nothing to say; silence must stay silence."""
    await _author(runtime, "quiet", QUIET_RUN)

    results = await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)

    assert [r.job for r in results if r.ran] == ["cap-quiet"]
    assert runtime.surface.sent == []


async def test_state_carries_between_scheduled_runs(runtime: Runtime) -> None:
    """A scheduled capability remembers where it got to across ticks."""
    await _author(runtime, "notifier", NOTIFY_RUN)
    await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)

    # a week later, same minute
    await runtime.scheduler.dispatch_tick(runtime, when=datetime(2026, 10, 23, 7, 30))

    manifest = runtime.capabilities.get("notifier")
    assert runtime.runner.read_state(manifest)["runs"] == 2
