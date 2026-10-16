"""Publishing to the tailnet, and never past it (SPEC §3.5, ticket T2).

*"Loopback-bound, no public Funnel."* The dashboard has two gates — the loopback
bind with a token on every write, and the tailnet's own authentication. Publishing
is handing the inner gate to the outer one. Three ways to get it wrong are worth
a test each:

- **A refusal that arrives as a traceback.** An owner who runs `--publish` on a
  laptop without `tailscale` should be told plainly, not handed a stack.
- **A public exposure.** `tailscale funnel` is the outer gate removed entirely;
  this module must not construct that command, and must refuse to publish onto a
  node that already has Funnel on.
- **A command nobody saw.** The line is printed before it runs — the owner is
  about to publish something, and is entitled to read what.
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


def _publish(fake: Fake, port: int = 8787) -> object:
    return publish(port, which=fake.which, run=fake.run)


# -- the refusal that is a message, not a traceback --------------------------


def test_a_missing_tailscale_is_a_clear_refusal() -> None:
    fake = Fake(installed=False)

    with pytest.raises(PublishError) as refused:
        _publish(fake)

    assert "not installed" in str(refused.value)
    assert fake.ran == [], "nothing was run before the check"


def test_a_failed_publish_command_is_reported_with_its_error() -> None:
    fake = Fake(fail_publish=True)

    with pytest.raises(PublishError) as refused:
        _publish(fake)

    assert "failed" in str(refused.value) and "boom" in str(refused.value)


# -- funnel is the gate removed ----------------------------------------------


def test_a_node_with_funnel_on_is_refused() -> None:
    fake = Fake(funnel_on=True)

    with pytest.raises(PublishError) as refused:
        _publish(fake)

    assert "Funnel" in str(refused.value)
    assert "public" in str(refused.value)
    assert [cmd[1:3] for cmd in fake.ran] == [["serve", "status"]], (
        "refused before anything was published"
    )


def test_a_node_without_funnel_publishes() -> None:
    published = _publish(Fake())

    assert published.url == "https://machine.tailnet.ts.net"


def test_an_unreadable_config_does_not_stop_the_publish() -> None:
    """A config we cannot read is not a funnel we can see; `serve` is tailnet-only."""
    fake = Fake(unparsable=True, serve_output="")

    published = _publish(fake)

    assert published.url == ""
    assert "serve" in published.command


# -- the command, seen before it is run --------------------------------------


def test_the_publish_command_is_serve_and_printed_first(capsys) -> None:
    published = _publish(Fake())

    printed = capsys.readouterr().out
    assert list(published.command) == ["tailscale", "serve", "--bg", "http://127.0.0.1:8787"]
    assert "+ tailscale serve --bg http://127.0.0.1:8787" in printed
    assert printed.index("+ tailscale serve --bg") < printed.index("https://"), (
        "the command is shown before the URL it earned"
    )


def test_the_only_commands_ever_built_are_serve_and_status() -> None:
    """No code path enables Funnel: the word is only ever read, never set."""
    fake = Fake()
    _publish(fake)
    refused = Fake(funnel_on=True)
    with pytest.raises(PublishError):
        _publish(refused)

    for command in [*fake.ran, *refused.ran]:
        assert command[0] == BIN
        assert command[1] == "serve", f"{command} is not a serve command"
        assert "--funnel" not in command
        assert "enable" not in command


def test_the_url_is_read_from_the_serve_status() -> None:
    fake = Fake(serve_output="https://box.tail1234.ts.net  (proxying to 127.0.0.1:8787)\n")

    published = _publish(fake)

    assert published.url == "https://box.tail1234.ts.net"


def test_no_url_in_the_status_still_publishes() -> None:
    fake = Fake(serve_output="(nothing served)\n")

    published = _publish(fake)

    assert published.url == ""
    assert published.command


# -- taking it back down -----------------------------------------------------


def test_unpublish_takes_the_route_down_and_says_so(capsys) -> None:
    fake = Fake()

    assert unpublish(8787, which=fake.which, run=fake.run) is True

    assert fake.ran[-1] == [BIN, "serve", "--https=443", "off"]
    assert "+ tailscale serve --https=443 off" in capsys.readouterr().out


def test_unpublish_without_tailscale_is_a_quiet_no() -> None:
    fake = Fake(installed=False)

    assert unpublish(8787, which=fake.which, run=fake.run) is False
    assert fake.ran == []


def test_unpublish_reports_a_failure_instead_of_lying() -> None:
    fake = Fake()

    def run(command: Sequence[str]) -> object:
        fake.ran.append(list(command))
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "not yours"})()

    assert unpublish(8787, which=fake.which, run=run) is False


# -- wiring ------------------------------------------------------------------


def test_the_flag_exists() -> None:
    from daedalus.main import build_parser

    assert build_parser().parse_args(["serve", "--publish"]).publish is True


def test_serve_refuses_to_publish_when_there_is_no_tailnet(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The acceptance: a missing `tailscale` is a clear refusal, not a traceback."""
    from daedalus.genui import publish as publish_module
    from daedalus.main import main

    def refused(*_: object, **__: object) -> object:
        raise PublishError(
            "tailscale is not installed, so there is no tailnet to publish to"
        )

    monkeypatch.setattr(publish_module, "publish", refused)

    code = main(["--home", str(tmp_path), "serve", "--port", "0", "--publish"])

    assert code == 1
    assert "not published" in capsys.readouterr().out
