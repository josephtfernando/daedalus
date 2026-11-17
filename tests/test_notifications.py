"""A scheduled capability that nobody is told about is not automation.

The run happens with no model in the loop, so if the notification does not reach
the surface, the system silently does nothing and reports success. The scheduler
turns what the capability printed into a message the owner reads, exactly once
per scheduled minute, with the owner's name for the job and the capability's own
words — and records the delivery so "did it deliver?" is answerable without
asking the owner.
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


async def test_a_scheduled_run_reaches_the_owner_once_and_remembers_its_state(
    runtime: Runtime,
) -> None:
    # a quiet run is still silence: most runs have nothing to say
    await _author(runtime, "quiet", QUIET_RUN)
    quiet_results = await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)
    assert [r.job for r in quiet_results if r.ran] == ["cap-quiet"]
    assert runtime.surface.sent == []

    # a notifying run reaches the owner with its own words, under their name
    await _author(runtime, "notifier", NOTIFY_RUN)
    results = await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)

    assert [r.job for r in results if r.ran] == ["cap-notifier"]
    assert runtime.surface.sent, "the notification never reached the owner"
    sent = runtime.surface.sent[-1]
    assert "Disk / is at 4.1% free." in sent, "the capability's own words were lost"
    assert "cap-notifier" not in sent, "the owner reads the name they chose, not our job handle"
    assert "notifier" in sent

    # a repeat tick in the same minute does not message twice
    again = await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)
    assert all(not r.ran for r in again), "the same minute fired twice"
    assert len(runtime.surface.sent) == 1, "the owner was messaged twice for one tick"

    # a week later, same minute, the capability remembers where it got to
    await runtime.scheduler.dispatch_tick(runtime, when=datetime(2026, 10, 23, 7, 30))
    manifest = runtime.capabilities.get("notifier")
    assert runtime.runner.read_state(manifest)["runs"] == 2


async def test_a_delivery_is_recorded_in_the_log(runtime: Runtime, caplog) -> None:
    """The delivered message is the one piece of evidence only the owner can see.

    A capability that speaks and a capability that silently fails both look like
    "exit 0" from the outside, so a scheduled delivery has to say so somewhere
    the operator can read.
    """
    import logging

    await _author(runtime, "notifier", NOTIFY_RUN)

    with caplog.at_level(logging.INFO, logger="daedalus.scheduler"):
        await runtime.scheduler.dispatch_tick(runtime, when=FRIDAY_0730)

    assert "capability notifier delivered a notification" in caplog.messages
