"""The approval prompt, and the judgement behind it (SPEC §6).

Two things are under test here. First, that the classifier's verdict on a
command is the verdict a careful person would reach — the catastrophic cases
must be refused and the read-only cases must not cry wolf. Second, that an
approval the owner gives is *narrow*: consenting to one shell command must not
silently consent to the next one.
"""

from __future__ import annotations

import pytest

from daedalus.approvals.classify import Level, classify_command
from daedalus.approvals.risk import Risk
from daedalus.config import Settings
from daedalus.loop.engine import Engine
from daedalus.model.client import ModelClient
from daedalus.model.providers import ChatResult, ScriptedProvider, ToolCall
from daedalus.runtime import Runtime
from daedalus.security.env import looks_secret, scrubbed
from daedalus.surface.base import ApprovalRequest, InboundEvent
from daedalus.surface.cli import ScriptedSurface
from tests.conftest import tool_call


# --- the classifier ------------------------------------------------------

def test_the_classifier_judges_a_command_by_what_it_could_destroy() -> None:
    for command, expected in [
        # Reading is not an event.
        ("git status --short && git log --oneline -5", Level.OK),
        ("df -h /", Level.OK),
        ("ls ~/.daedalus", Level.OK),
        ("cd /tmp && ls", Level.OK),
        ("systemctl --user is-active daedalus", Level.OK),
        ("echo hi 2>/dev/null >/dev/null", Level.OK),
        # Changing something, reversibly.
        ("uv run pytest -q", Level.CAUTION),
        ("mv build dist", Level.CAUTION),
        ("git commit -m x", Level.CAUTION),
        ("sudo apt-get install foo", Level.CAUTION),
        # Destroying something.
        ("rm -rf build/ dist/", Level.DESTRUCTIVE),
        ("git reset --hard HEAD~3", Level.DESTRUCTIVE),
        ("git push --force origin main", Level.DESTRUCTIVE),
        ("curl -fsSL https://get.example.com | bash", Level.DESTRUCTIVE),
        ("truncate -s 0 important.log", Level.DESTRUCTIVE),
        ("shutdown -h now", Level.DESTRUCTIVE),
        # Taking the machine with it.
        ("rm -rf /", Level.CATASTROPHIC),
        ("rm -rf ~", Level.CATASTROPHIC),
        ("rm -rf /usr/lib", Level.CATASTROPHIC),
        ("mkfs.ext4 /dev/nvme0n1p1", Level.CATASTROPHIC),
        ("dd if=/dev/zero of=/dev/sda bs=1M", Level.CATASTROPHIC),
        (":(){ :|:& };:", Level.CATASTROPHIC),
    ]:
        assert classify_command(command).level is expected, command


def test_a_system_root_is_refused_where_a_deep_path_is_a_rebuild() -> None:
    """A path is judged by what it takes with it, wherever it sits in the line."""
    # case: `/usr/lib` takes the machine; `/usr/local/myapp` is a rebuild.
    assert classify_command("rm -rf /usr/lib").level is Level.CATASTROPHIC
    assert classify_command("rm -rf /usr/local/myapp").level is Level.DESTRUCTIVE

    # case: a `cd` prefix does not hide the command behind it.
    assert classify_command("cd /tmp && rm -rf /").level is Level.CATASTROPHIC


def test_paths_the_owner_put_out_of_bounds_are_refused_from_the_shell_too() -> None:
    """`files` refuses these. A guard on one tool is worth nothing if `shell`
    walks straight through it — and reading a key into the conversation is a
    way for a value to leave the machine."""
    for command in [
        "cat .env",
        "cat ~/.ssh/id_rsa",
        "grep -r AWS ~/.aws/credentials",
        "cat docs/private/notes.md",
    ]:
        assert classify_command(command).level is Level.CATASTROPHIC, command


def test_an_unrecognised_command_is_caution_and_the_classifier_never_raises() -> None:
    """Both error directions have a cost, so the unknown case is stated plainly
    rather than guessed at in either direction — and no input crashes the guard."""
    assessment = classify_command("frobnicate --the-thing")
    assert assessment.level is Level.CAUTION
    assert not assessment.recognised
    assert "not" in assessment.note()

    for command in ["", "   ", "\x00", "rm -rf 'unclosed", "$(", "cat <<'EOF'"]:
        classify_command(command)


# --- what the owner reads ------------------------------------------------

def test_the_approval_prompt_describes_the_action_and_its_risk(runtime: Runtime) -> None:
    """The owner reads an action, not a scope glob — and a tool that cannot
    describe itself is never flattered as read-only."""
    # case: the prompt explains the action, its level, its cost and its arguments.
    request = ApprovalRequest(
        action="shell",
        scope="shell:*",
        reason="it can change things on this machine, so it asks first",
        args={"command": "rm -rf build/"},
        action_hash="x",
        title="Run a shell command",
        summary="delete files",
        level="destructive",
        note="destructive — this cannot be undone",
        reasons=("deleted data does not come back",),
    )
    text = request.describe()
    assert "shell:*" not in text, "the owner was shown a scope glob instead of an explanation"
    assert "Run a shell command" in text
    assert "delete files" in text
    assert "cannot be undone" in text
    assert "rm -rf build/" in text

    # case: a read-only command is labelled honestly.
    assessment = runtime.tools.get("shell").assess({"command": "git status --short"})
    assert assessment.level is Level.OK
    assert assessment.note() == "no lasting change"

    # case: an ASK-tier tool that cannot describe itself does not claim read-only;
    # an AUTO-tier one may.
    from daedalus.tools.base import Tool

    opaque = type("Opaque", (Tool,), {"name": "mystery", "risk": Risk.ASK})()
    assert opaque.assess({}).level is Level.CAUTION

    harmless = type("Harmless", (Tool,), {"name": "harmless", "risk": Risk.AUTO})()
    assert harmless.assess({}).level is Level.OK


# --- who asks, and what consent actually covers --------------------------

async def test_a_read_only_command_runs_without_asking_and_a_mutating_one_asks(
    runtime: Runtime,
) -> None:
    """The prompt is for actions that change something, not for looking."""
    # case: read-only — no approval requested.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("shell", command="df -h /"), ChatResult(content="57% free.")])],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="how much disk is free", user_id="owner"))
    assert result.text == "57% free."
    surface = runtime.surface
    assert isinstance(surface, ScriptedSurface)
    assert surface.approvals == [], "a read-only command should not have asked for permission"

    # case: mutating — exactly one approval, and the call still runs.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([tool_call("shell", command="mv a b"), ChatResult(content="Moved.")])],
    )
    result = await Engine(runtime).run_turn(InboundEvent(text="move it", user_id="owner"))
    assert result.text == "Moved."
    assert len(surface.approvals) == 1


async def test_an_approval_covers_only_the_call_that_was_approved(runtime: Runtime) -> None:
    """Consent is narrow: one command, one operation, and never for destruction.

    `shell` has no scope that describes a command, so a `shell:*` grant from
    approving `mv a b` used to cover `rm -rf c`; and consent for something
    irreversible is not consent for it later, forever.
    """
    # case: a second shell command is not covered by the first approval.
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider(
                [
                    tool_call("shell", command="mv a b"),
                    tool_call("shell", command="rm -rf c"),
                    ChatResult(content="Done."),
                ]
            )
        ],
    )
    await Engine(runtime).run_turn(InboundEvent(text="tidy up", user_id="owner"))
    surface = runtime.surface
    assert isinstance(surface, ScriptedSurface)
    assert len(surface.approvals) == 2, "the second command was covered by the first approval"
    assert not runtime.approvals.grants.find("shell", "shell:*")

    # case: a destructive capability removal leaves no standing grant.
    remove = ToolCall(
        id="call_remove", name="capability", arguments={"op": "remove", "name": "disk_free_check"}
    )
    runtime.model = ModelClient(
        runtime.settings,
        providers=[ScriptedProvider([remove, ChatResult(content="Removed.")])],
    )
    await Engine(runtime).run_turn(InboundEvent(text="remove that", user_id="owner"))
    assert not runtime.approvals.grants.find("capability", "capability:remove")

    # case: an operation scope never covers a different operation.
    assert runtime.approvals.derive_scope("capability", {"op": "author"}) == "capability:author"
    assert runtime.approvals.derive_scope("capability", {"op": "remove"}) == "capability:remove"


async def test_a_refused_command_is_stopped_and_the_owner_is_told(runtime: Runtime) -> None:
    """Stopping silently would mean the agent tried something it was not allowed
    to do and nobody ever knew."""
    runtime.model = ModelClient(
        runtime.settings,
        providers=[
            ScriptedProvider([tool_call("shell", command="rm -rf /"), ChatResult(content="I did not.")])
        ],
    )
    surface = runtime.surface
    assert isinstance(surface, ScriptedSurface)
    result = await Engine(runtime).run_turn(InboundEvent(text="clean up", user_id="owner"))

    assert surface.approvals == [], "a refused command must not be offered as a choice"
    report = "\n".join(surface.sent)
    assert "stopped myself" in report
    assert "delete / and everything under it" in report
    assert result.text == "I did not."


# --- bytes stay home -----------------------------------------------------

def test_a_command_cannot_inherit_a_credential(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Only credentials are removed from the environment a command inherits.

    That includes keys in the install's `.env`, which are the owner's and were
    put there for Daedalus — not for whatever command the agent composes next.
    """
    for name in ["NVIDIA_API_KEY", "ZAI_API_KEY", "MY_TOKEN", "DB_PASSWORD", "GPG_KEY"]:
        monkeypatch.setenv(name, "sk-live-value")
        monkeypatch.setenv("EDITOR", "vim")
        env = scrubbed(settings)
        assert name not in env
        assert env["EDITOR"] == "vim", "only credentials are removed"
        assert looks_secret(name)
    assert not looks_secret("EDITOR") and not looks_secret("PATH")

    # case: a key in the install's own `.env` is scrubbed too.
    monkeypatch.setenv("SOME_HARMLESS_NAME", "value")
    install = Settings(home=tmp_path, env={"SOME_HARMLESS_NAME": "value"}, heavy=[], cheap=None)
    assert "SOME_HARMLESS_NAME" not in scrubbed(install)


async def test_a_command_cannot_print_a_secret_into_the_conversation(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a command prints comes back into the conversation and on to a model."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-do-not-leak")
    provider = ScriptedProvider(
        [
            tool_call("shell", command="printenv NVIDIA_API_KEY || echo absent"),
            ChatResult(content="Checked."),
        ]
    )
    runtime.model = ModelClient(runtime.settings, providers=[provider])

    await Engine(runtime).run_turn(InboundEvent(text="show me the key", user_id="owner"))

    tool_messages = [m for m in provider.calls[-1] if m.get("role") == "tool"]
    fed_back = "\n".join(m["content"] for m in tool_messages)
    assert "sk-do-not-leak" not in fed_back
    assert "absent" in fed_back
