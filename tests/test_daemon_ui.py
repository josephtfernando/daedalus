"""The daemon's dashboard: a fixed port, and a route that comes down with it.

The bug this file exists for is a *silent* one. The dashboard bound port 0, so
every run chose a number nobody else knew — while `tailscale serve` had installed
a route to the number a *previous* run chose. The owner's phone then reached a
port with nothing on it and got a connection error, and nothing anywhere said
why: the daemon log called it "loopback only" and the route still existed.

So these tests are about the three things that have to hold for "from the phone"
to mean anything:

- the port is **fixed**, so a route stays valid across restarts;
- publishing happens **when the daemon starts**, which is what heals a route left
  pointing at a dead port by a killed run;
- the route comes **down before the server stops**, so there is no window where
  the tailnet points at a port that has already stopped answering.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from daedalus import genui
from daedalus.config import Settings, default_config_toml, load_settings
from daedalus.genui.publish import PublishError
from daedalus.main import _publish_dashboard, _start_dashboard, _stop_dashboard
from daedalus.runtime import Runtime
from daedalus.storage import paths


class Route:
    """A stand-in `tailscale serve`: records what was asked of it."""

    def __init__(self, *, url: str = "https://delphi.example.ts.net", fail: bool = False) -> None:
        self.url = url
        self.fail = fail
        self.published: list[int] = []
        self.unpublished: list[int] = []

    def publish(self, port: int, **_: object):
        if self.fail:
            raise PublishError("tailscale is not installed, so there is no tailnet to publish to")
        self.published.append(int(port))
        return type("Published", (), {"url": self.url, "command": (), "ran": ()})()

    def unpublish(self, port: int, **_: object) -> bool:
        self.unpublished.append(int(port))
        return True


@pytest.fixture()
def route(monkeypatch: pytest.MonkeyPatch) -> Route:
    """Install a fake route, and keep the real `tailscale` out of the test."""
    fake = Route()
    monkeypatch.setattr(genui.publish, "publish", fake.publish)
    monkeypatch.setattr(genui.publish, "unpublish", fake.unpublish)
    return fake


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start(rt: Runtime):
    """Start the dashboard the way the daemon does — inside a running loop.

    The controls carry a reference to the loop they will hand work back to, so
    starting them outside one is a different code path, not a shortcut.
    """

    async def go():
        return _start_dashboard(rt)

    return asyncio.run(go())


def written_config(home: Path) -> Path:
    paths.ensure_tree(home)
    config = home / "config.toml"
    config.write_text(default_config_toml(), encoding="utf-8")
    return config


# -- the configuration -------------------------------------------------------


def test_the_dashboard_port_is_fixed_by_default(tmp_path: Path) -> None:
    from daedalus.genui.server import DEFAULT_PORT

    home = tmp_path / "home"
    config = written_config(home)
    settings = load_settings(home)
    assert settings.dashboard_port == DEFAULT_PORT
    assert settings.publish_dashboard is True
    assert "[ui]" in config.read_text(encoding="utf-8")


def test_the_owner_can_move_the_port_and_turn_publishing_off(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = written_config(home)
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace("port = 8787", "port = 9123")
        .replace("publish = true", "publish = false"),
        encoding="utf-8",
    )
    settings = load_settings(home)
    assert settings.dashboard_port == 9123
    assert settings.publish_dashboard is False


# -- the daemon --------------------------------------------------------------


def test_the_dashboard_binds_the_port_the_route_will_point_at(runtime: Runtime) -> None:
    """The whole bug in one assertion: the port is the configured one, not a new one."""
    runtime.settings.dashboard_port = free_port()
    runtime.settings.publish_dashboard = False
    dashboard = start(runtime)
    try:
        assert dashboard is not None
        assert dashboard.port == runtime.settings.dashboard_port
    finally:
        _stop_dashboard(runtime, dashboard)


def test_the_daemon_publishes_the_dashboard_it_started(runtime: Runtime, route: Route) -> None:
    runtime.settings.dashboard_port = free_port()
    dashboard = start(runtime)
    try:
        assert route.published == [dashboard.port]
    finally:
        _stop_dashboard(runtime, dashboard)


def test_the_route_comes_down_before_the_server_stops(runtime: Runtime, route: Route) -> None:
    """Otherwise the tailnet points at a port that stopped answering a moment ago."""
    events: list[str] = []
    runtime.settings.publish_dashboard = True

    class Order:
        port = 4321

        def stop(self) -> None:
            events.append("stop")

    original = route.unpublish

    def unpublish(port: int, **_: object) -> bool:
        events.append("unpublish")
        return original(port)

    genui.publish.unpublish = unpublish  # type: ignore[assignment]
    _stop_dashboard(runtime, Order())  # type: ignore[arg-type]
    assert events == ["unpublish", "stop"]


def test_a_refused_publish_is_reported_and_not_fatal(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `tailscale` on the machine must not cost the owner their daemon."""
    monkeypatch.setattr(genui.publish, "publish", Route(fail=True).publish)
    runtime.settings.publish_dashboard = True
    _publish_dashboard(runtime, 8787)  # must not raise
    assert "not published" in capsys.readouterr().out


def test_publishing_is_off_when_the_owner_turns_it_off(
    runtime: Runtime, route: Route, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime.settings.publish_dashboard = False
    _publish_dashboard(runtime, 8787)
    assert route.published == []
    assert "loopback only" in capsys.readouterr().out


def test_a_busy_port_is_reported_rather_than_published_over(
    runtime: Runtime, route: Route, capsys: pytest.CaptureFixture[str]
) -> None:
    """Something else already answers on the number the route points at.

    Publishing over it would send the owner to the wrong process, so the daemon
    refuses the dashboard instead and says so.
    """
    port = free_port()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen(1)
        runtime.settings.dashboard_port = port
        dashboard = start(runtime)
    assert dashboard is None
    assert route.published == []
    assert "did not start" in capsys.readouterr().out


def test_a_dashboard_without_controls_still_stops_cleanly(settings: Settings) -> None:
    """`_stop_dashboard` is called on every exit path, including the ones with nothing."""
    _stop_dashboard(type("R", (), {"settings": settings})(), None)  # type: ignore[arg-type]
