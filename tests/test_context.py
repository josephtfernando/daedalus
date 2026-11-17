"""What the model is told before it answers (ARCHITECTURE §7.1).

The prompt is the only place a fact like *today's date* can come from. Nothing in
the loop, the tool schemas or a tool result carries it, and no care taken inside a
tool recovers a date the model invented on the way in.

On 17 Oct the owner asked for a reminder "on Sunday, which is tomorrow". The agent
answered with **10 Oct 2026** — a Saturday, seven days gone — and the calendar took
it, because a start in the past is a perfectly legal write. Two events then sat on
a day nobody had mentioned, and the owner was the only person who could see them.
"""

from __future__ import annotations

from datetime import date, datetime

from daedalus.loop.context import build_system_prompt, now_block
from daedalus.runtime import Runtime
from daedalus.surface.base import InboundEvent


def test_the_moment_is_spelled_out_and_the_week_does_not_move_at_midnight() -> None:
    """A fixed moment, so what the block says cannot drift with the calendar.

    Sunday night is still the week's last day, and tomorrow is the next Monday:
    "this Sunday" and "next Monday" mean different weeks, and an agent working
    that out from a bare date gets it wrong exactly at the boundary.
    """
    # case: a mid-morning moment, every phrase the owner would use.
    block = now_block(datetime(2026, 10, 17, 9, 4))
    assert block.startswith("It is 09:04 on Saturday 17 October 2026"), block
    assert "Today is Sat 17 Oct." in block
    assert "Tomorrow is Sun 18 Oct; yesterday was Fri 16 Oct." in block
    assert "This week runs Mon 12 Oct to Sun 18 Oct." in block

    # case: Sunday 23:30 — the week has not rolled over with the day.
    block = now_block(datetime(2026, 10, 18, 23, 30))
    assert "Today is Sun 18 Oct. Tomorrow is Mon 19 Oct; yesterday was Sat 17 Oct." in block
    assert "This week runs Mon 12 Oct to Sun 18 Oct." in block


def test_the_assembled_prompt_carries_the_date_and_the_capture_rule(runtime: Runtime) -> None:
    """Not just the helper — the prompt the model actually reads.

    The date must be there or every relative date the owner gives is a guess the
    model cannot check. And a durable fact only becomes memory if the prompt asks
    for it to: on 18 Oct the owner told Calliope a fact about his own writing and
    got an answer that kept none of it, because no instruction anywhere made
    capture part of the turn.
    """
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))

    today = date.today()
    assert "## Right now" in prompt
    assert f"Today is {today.strftime('%a %d %b')}." in prompt, (
        "the agent was not told what today is — every relative date it is given "
        "is then a guess it cannot check"
    )

    assert "### Remembering" in prompt
    assert "memory op=write" in prompt, (
        "the capture rule must name the call, or the model reads it as a feeling"
    )
    assert "supersede" in prompt, (
        "capture without supersession writes contradictions beside facts, and "
        "the next turn cannot tell which the owner meant"
    )
    for facet in ("people", "contacts"):
        assert facet in prompt, f"the suggested facets must be reachable by the model ({facet})"


def test_the_clock_rides_in_the_turn_so_the_prefix_can_be_cached(runtime: Runtime) -> None:
    """The minute must not be in the system prompt.

    llama.cpp reuses the KV of an identical prefix. The system prompt used to
    open with `It is 21:20 on …`, so its first bytes changed every sixty seconds
    and no prefix was ever reused; measured through /v1/chat/completions on the
    owner's laptop, that cost 20.2 s of prefill on a turn that a warm prefix
    answers in 2.9 s. The date stays (it changes daily, not minutely), the
    minute moves to the user message, and this test pins the split.
    """
    import re

    from daedalus.loop.context import build_messages, clock_block, date_block

    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))
    assert not re.search(r"\bIt is \d{2}:\d{2}\b", prompt), (
        "the clock is back in the system prompt — every turn now pays full prefill"
    )
    assert date_block().splitlines()[0] in prompt, "the day must still be told"

    event = InboundEvent(text="hi", user_id="owner")
    messages = build_messages(runtime, event)
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"].startswith(clock_block()), (
        "the minute belongs to the turn, next to the message that needs it"
    )
    assert messages[-1]["content"].endswith("hi")
