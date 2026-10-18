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


def test_the_moment_is_spelled_out_in_the_words_an_owner_uses() -> None:
    """A fixed moment, so what the block says cannot drift with the calendar."""
    block = now_block(datetime(2026, 10, 17, 9, 4))

    assert block.startswith("It is 09:04 on Saturday 17 October 2026"), block
    assert "Today is Sat 17 Oct." in block
    assert "Tomorrow is Sun 18 Oct; yesterday was Fri 16 Oct." in block
    assert "This week runs Mon 12 Oct to Sun 18 Oct." in block


def test_the_week_does_not_move_when_the_day_rolls_over() -> None:
    """Sunday night is still the week's last day, and tomorrow is the next Monday.

    "This Sunday" and "next Monday" mean different weeks, and an agent that has to
    work that out from a bare date gets it wrong exactly when it matters — at the
    boundary, which is when an owner says "tomorrow" and means the other week.
    """
    block = now_block(datetime(2026, 10, 18, 23, 30))

    assert "Today is Sun 18 Oct. Tomorrow is Mon 19 Oct; yesterday was Sat 17 Oct." in block
    assert "This week runs Mon 12 Oct to Sun 18 Oct." in block


def test_the_prompt_the_agent_is_actually_given_carries_the_date(runtime: Runtime) -> None:
    """Not just the helper — the assembled prompt, which is what the model reads."""
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))

    today = date.today()
    assert "## Right now" in prompt
    assert f"Today is {today.strftime('%a %d %b')}." in prompt, (
        "the agent was not told what today is — every relative date it is given "
        "is then a guess it cannot check"
    )


def test_the_prompt_tells_the_model_what_capture_is_for(runtime: Runtime) -> None:
    """A durable fact only becomes memory if something asks for it to.

    Nothing recalls for the owner: the store sits behind a tool the model must
    choose to use, and nothing else in the prompt, the schemas or a tool result
    says *when* to use it. So on 18 Oct the owner told Calliope a fact about his
    own writing and got an answer that kept none of it — not because memory was
    missing, but because no instruction anywhere made capture part of the turn.
    The rule is written where the other behavioural contracts live: in the
    prompt. The store, supersession and the `local_only` gate it relies on are
    §5's; this only names the habit.
    """
    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))

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
