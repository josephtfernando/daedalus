"""Publishing to the tailnet, and never past it (SPEC §3.5, ticket T2).

*"Loopback-bound, no public Funnel."* Publishing is handing the inner gate to the
tailnet's own authentication. Three ways to get it wrong are worth a test each:

- **A refusal that arrives as a traceback.** An owner who runs `--publish` on a
  laptop without `tailscale` should be told plainly, not handed a stack.
- **A public exposure.** `tailscale funnel` is the outer gate removed entirely;
  this module must not construct that command, and must refuse to publish onto a
  node that already has Funnel on.
- **A command nobody saw.** The line is printed before it runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pytest

from daedalus.genui.publish import BIN, PublishError, publish, unpublish


@dataclass
class Fake:
    """A `tailscale` binary, as the tests would like it to behave."""

    installed: bool = True
    funnel_on: bool = False
    serve_output: str = "https://machine.tailnet.ts.net\n"
    fail_publish: bool = False
    port: int = 8787
    unparsable: bool = False
    ran: list[list[str]] = field(default_factory=list)

    def which(self, name: str) -> str | None:
        return "/usr/bin/tailscale" if (self.installed and name == BIN) else None

    def run(self, command: Sequence[str]) -> object:
        self.ran.append(list(command))
        args = list(command)

        def out(code: int, stdout: str = "", stderr: str = "") -> object:
            return type("R", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()

        if args[1:] == ["serve", "status", "--json"]:
            if self.unparsable:
                return out(0, "not json at all")
            status: dict = {"Web": {}, "Funnel": {}}
            if self.funnel_on:
                status["Funnel"] = {"machine.tailnet.ts.net:443": {"Handlers": {}}}
            return out(0, json.dumps(status))
        if args[1:] == ["serve", "status"]:
            return out(0, self.serve_output)
        if args[1:] == ["serve", "--bg", f"http://127.0.0.1:{self.port}"]:
            return out(1 if self.fail_publish else 0, "", "" if not self.fail_publish else "boom")
        if args[1:] == ["serve", "--https=443", "off"]:
            return out(0)
        return out(0, "")


def _publish(fake: Fake) -> object:
    return publish(8787, which=fake.which, run=fake.run)


def test_a_missing_tailscale_is_a_clear_refusal_not_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # the module refuses plainly, and nothing is run before the check
    fake = Fake(installed=False)
    with pytest.raises(PublishError) as refused:
        _publish(fake)
    assert "not installed" in str(refused.value)
    assert fake.ran == [], "nothing was run before the check"

    # a publish command that fails is reported with its own error
    failing = Fake(fail_publish=True)
    with pytest.raises(PublishError) as failed:
        _publish(failing)
    assert "failed" in str(failed.value) and "boom" in str(failed.value)

    # the acceptance: `serve --publish` turns that into a message and exit 1
    from daedalus.genui import publish as publish_module
    from daedalus.main import build_parser, main

    assert build_parser().parse_args(["serve", "--publish"]).publish is True

    def denied(*_: object, **__: object) -> object:
        raise PublishError(
            "tailscale is not installed, so there is no tailnet to publish to"
        )

    monkeypatch.setattr(publish_module, "publish", denied)
    code = main(["--home", str(tmp_path), "serve", "--port", "0", "--publish"])
    assert code == 1
    assert "not published" in capsys.readouterr().out


def test_a_public_funnel_is_refused_and_never_enabled() -> None:
    # Funnel on is the outer gate removed: refused before anything is published
    refused = Fake(funnel_on=True)
    with pytest.raises(PublishError) as err:
        _publish(refused)
    assert "Funnel" in str(err.value)
    assert "public" in str(err.value)
    assert [cmd[1:3] for cmd in refused.ran] == [["serve", "status"]], (
        "refused before anything was published"
    )

    # without Funnel the node publishes, and the URL is read from serve status
    plain = _publish(Fake())
    assert plain.url == "https://machine.tailnet.ts.net"
    resolved = _publish(
        Fake(serve_output="https://box.tail1234.ts.net  (proxying to 127.0.0.1:8787)\n")
    )
    assert resolved.url == "https://box.tail1234.ts.net"

    # an unreadable config is not a funnel we can see; a status with no URL still
    # publishes, because `serve` is tailnet-only either way
    unreadable = _publish(Fake(unparsable=True, serve_output=""))
    assert unreadable.url == ""
    assert "serve" in unreadable.command
    empty = _publish(Fake(serve_output="(nothing served)\n"))
    assert empty.url == ""
    assert empty.command

    # and no code path ever enables Funnel: the word is only ever read, never set
    for command in [*plain.ran, *resolved.ran, *unreadable.ran, *empty.ran, *refused.ran]:
        assert command[0] == BIN
        assert command[1] == "serve", f"{command} is not a serve command"
        assert "--funnel" not in command
        assert "enable" not in command


def test_the_command_is_printed_before_it_runs(capsys) -> None:
    published = _publish(Fake())

    printed = capsys.readouterr().out
    assert list(published.command) == ["tailscale", "serve", "--bg", "http://127.0.0.1:8787"]
    assert "+ tailscale serve --bg http://127.0.0.1:8787" in printed
    assert printed.index("+ tailscale serve --bg") < printed.index("https://"), (
        "the command is shown before the URL it earned"
    )


def test_unpublish_takes_the_route_down_or_says_no(capsys) -> None:
    fake = Fake()
    assert unpublish(8787, which=fake.which, run=fake.run) is True
    assert fake.ran[-1] == [BIN, "serve", "--https=443", "off"]
    assert "+ tailscale serve --https=443 off" in capsys.readouterr().out

    # without tailscale it is a quiet no, and nothing runs
    absent = Fake(installed=False)
    assert unpublish(8787, which=absent.which, run=absent.run) is False
    assert absent.ran == []

    # a failing command is reported instead of lied about
    failing = Fake()

    def run(command: Sequence[str]) -> object:
        failing.ran.append(list(command))
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "not yours"})()

    assert unpublish(8787, which=failing.which, run=run) is False
