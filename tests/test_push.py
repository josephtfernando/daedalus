"""The published artifact (T25, T34) and the Taildrop leg (T15).

One mechanism, tested from both sides: the registry's gate (one-time spend,
expiry, takedown, token) and the server's route that enforces it, then the
tool that reaches both — including the containment clause that publishing
cannot read where the files primitive cannot, and the push that names a
device rather than copying into the air. (The dashboard publish's own tests —
the Funnel refusal, the printed command — live in test_publish.py; those test
`genui/publish.py`, this tests what travels over the route it publishes.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.genui.publications import Publications
from daedalus.genui.server import LocalServer
from daedalus.tools.publish import PushTool


@pytest.fixture()
def pubs(tmp_path: Path) -> Publications:
    return Publications(tmp_path / "serve" / "published.json")


def server(tmp_path: Path, pubs: Publications) -> LocalServer:
    return LocalServer(
        tmp_path / "home",
        publications=pubs,
        artifacts=tmp_path / "home" / "ui",
    )


def staged(tmp_path: Path, body: bytes = b"payload") -> Path:
    serve = tmp_path / "home" / "serve"
    serve.mkdir(parents=True, exist_ok=True)
    (serve / "brief.txt").write_bytes(body)
    return serve / "brief.txt"


def test_a_one_time_link_spends_on_first_fetch(pubs: Publications) -> None:
    pub = pubs.publish("brief.txt")
    assert pub.max_fetches == 1
    ok, reason, _ = pubs.admit("brief.txt", pub.token)
    assert (ok, reason) == (True, "")
    spent, spent_reason, _ = pubs.admit("brief.txt", pub.token)
    assert not spent and "already fetched" in spent_reason


def test_the_route_serves_once_and_then_refuses(pubs: Publications, tmp_path: Path) -> None:
    pub = pubs.publish("brief.txt")
    staged(tmp_path)
    srv = server(tmp_path, pubs)

    first = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert first.status == 200 and first.body == b"payload"
    assert dict(first.headers)["Cache-Control"] == "no-store", (
        "a one-time link served from browser cache was never one-time"
    )

    second = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert second.status == 410, "the second open finds the gate closed, honestly named"


def test_the_wrong_token_refuses_and_does_not_spend(pubs: Publications, tmp_path: Path) -> None:
    pub = pubs.publish("brief.txt")
    staged(tmp_path)
    srv = server(tmp_path, pubs)

    wrong = srv.get("/pub/brief.txt?t=not-this-one")
    assert wrong.status == 403
    assert pubs.get("brief.txt").fetches == 0, "a wrong token must not spend the link"
    right = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert right.status == 200


def test_expiry_is_a_read_path_fact(pubs: Publications, tmp_path: Path) -> None:
    import time

    pub = pubs.publish("brief.txt", lifetime_s=0.05)
    time.sleep(0.06)  # the lifetime runs out while nobody is sweeping
    staged(tmp_path)
    srv = server(tmp_path, pubs)
    answer = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert answer.status == 410 and "expired" in answer.text


def test_takedown_marks_the_record_dead(pubs: Publications, tmp_path: Path) -> None:
    pub = pubs.publish("brief.txt")
    staged(tmp_path)
    srv = server(tmp_path, pubs)

    assert pubs.takedown("brief.txt") is True
    assert pubs.takedown("brief.txt") is False, "a takedown is idempotent — not a second act"
    assert pubs.get("brief.txt").taken_down
    answer = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert answer.status == 410 and "taken down" in answer.text


def test_the_tool_takedown_deletes_the_bytes(runtime) -> None:
    """Deletion is the tool's act, next to the registry's mark."""
    import asyncio

    served = runtime.settings.home / "serve" / "x.txt"
    served.parent.mkdir(parents=True, exist_ok=True)
    served.write_bytes(b"payload")
    runtime.publications.publish("x.txt")
    result = asyncio.run(PushTool().run({"op": "takedown", "name": "x.txt"}, runtime))
    assert result.ok and not served.exists(), (
        "takedown deletes the bytes — a dead link with live bytes is a leak in waiting"
    )


def test_names_are_plain_and_republish_is_governed(pubs: Publications) -> None:
    with pytest.raises(ValueError):
        pubs.publish("../escape")
    with pytest.raises(ValueError):
        pubs.publish("a/b")
    with pytest.raises(ValueError):
        pubs.publish(r"..\win")
    pubs.publish("live.txt")
    with pytest.raises(ValueError):
        pubs.publish("live.txt"), "an alive publication is not replaced by a second link"
    pubs.takedown("live.txt")
    pubs.publish("live.txt")  # a dead one may be replaced


def test_a_server_without_publications_says_nothing(pubs, tmp_path) -> None:
    bare = LocalServer(tmp_path / "home", artifacts=tmp_path / "home" / "ui")
    assert bare.get("/pub/anything?t=x").status == 404, (
        "a server without publications answers as if nothing, not as if closed"
    )


async def test_the_tool_serves_within_the_files_tool_reach(runtime, monkeypatch) -> None:
    """The read is a files read: private paths are refused, home files publish."""
    from daedalus.tools import publish as publish_module

    monkeypatch.setattr(publish_module, "funnel_on", lambda: False)
    tool = PushTool()
    allowed = runtime.settings.home / "brief.txt"
    allowed.write_bytes(b"payload")
    result = await tool.run({"op": "serve", "path": str(allowed)}, runtime)
    assert result.ok, result.output
    record = runtime.publications.get(allowed.name)
    assert record is not None and record.fetches == 0
    copied = runtime.settings.home / "serve" / allowed.name
    assert copied.is_file(), "the publication's bytes live in serve/, not in ui/"

    blocked = runtime.settings.home / "docs" / "private" / "note.txt"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("x")
    refused = await tool.run({"op": "serve", "path": str(blocked)}, runtime)
    assert not refused.ok and "protected" in refused.output

    big = runtime.settings.home / "big.bin"
    big.write_bytes(b"\0" * (65 * 1024 * 1024))
    oversized = await tool.run({"op": "serve", "path": str(big)}, runtime)
    assert not oversized.ok and "publication cap" in oversized.output


async def test_publication_is_refused_in_front_of_a_funnel(runtime, monkeypatch) -> None:
    """T25's gate, inherited: Funnel on is a refusal, unknown is one too."""
    from daedalus.tools import publish as publish_module

    tool = PushTool()
    src = runtime.settings.home / "brief.txt"
    src.write_bytes(b"payload")
    monkeypatch.setattr(publish_module, "funnel_on", lambda: True)
    funnelled = await tool.run({"op": "serve", "path": str(src)}, runtime)
    assert not funnelled.ok and "Funnel" in funnelled.output and "refused" in funnelled.output
    assert not (runtime.settings.home / "serve" / src.name).exists(), (
        "a refused publish copies nothing — refusal precedes the move"
    )
    monkeypatch.setattr(publish_module, "funnel_on", lambda: None)
    unknown = await tool.run({"op": "serve", "path": str(src)}, runtime)
    assert not unknown.ok and "unknown" in unknown.output


async def test_the_tool_serves_a_url_with_the_token_and_a_takedown(runtime, monkeypatch) -> None:
    from daedalus.tools import publish as publish_module

    stub = runtime.settings.home / "stubts"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "tailscale").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = status ]; then echo \'{"Self": {"DNSName": "delphi.tailNET.ts.net."}}\'; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (stub / "tailscale").chmod(0o755)
    monkeypatch.setattr(publish_module, "BIN", str(stub / "tailscale"))
    monkeypatch.setattr(publish_module, "funnel_on", lambda: False)

    tool = PushTool()
    src = runtime.settings.home / "brief.txt"
    src.write_bytes(b"payload")
    result = await tool.run({"op": "serve", "path": str(src)}, runtime)
    assert result.ok, result.output
    link = result.data["link"]
    assert "https://delphi.tailNET.ts.net/pub/" in link and "?t=" in link
    token = link.split("?t=", 1)[1]
    assert runtime.publications.get(src.name).token == token

    # takedown: the link dies and the bytes go with it
    after = await tool.run({"op": "takedown", "name": src.name}, runtime)
    assert after.ok and not (runtime.settings.home / "serve" / src.name).exists()


async def test_push_names_a_peer_and_refuses_the_air(runtime, monkeypatch) -> None:
    from daedalus.tools import publish as publish_module

    stub = runtime.settings.home / "stubts2"
    stub.mkdir(parents=True, exist_ok=True)
    log = runtime.settings.home / "ts.log"
    log.write_text("")
    (stub / "tailscale").write_text(
        "#!/bin/sh\n"
        'if [ "$2" = cp ] && [ "$3" = "--targets" ]; then echo "phone:"; exit 0; fi\n'
        f'if [ "$2" = cp ]; then echo "argv: $@" >> "{log}"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (stub / "tailscale").chmod(0o755)
    monkeypatch.setattr(publish_module, "BIN", str(stub / "tailscale"))

    tool = PushTool()
    peers = await tool.run({"op": "peers"}, runtime)
    assert peers.ok and "phone" in peers.output

    refused = await tool.run({"op": "push", "path": "/tmp", "target": "not-a-peer"}, runtime)
    assert not refused.ok and "not a reachable tailnet peer" in refused.output

    src = runtime.settings.home / "brief.txt"
    src.write_bytes(b"payload")
    ok = await tool.run({"op": "push", "path": str(src), "target": "phone"}, runtime)
    assert ok.ok, ok.output
    assert "file cp" in log.read_text(), (
        "the copy went through the one Taildrop command, to a named peer"
    )


def test_push_assess_cautions_the_public_acts(runtime) -> None:
    from daedalus.approvals.classify import Level

    tool = PushTool()
    assert tool.risk_for({"op": "serve"}) is not None
    assert tool.assess({"op": "serve"}).level == Level.CAUTION
    assert tool.assess({"op": "push"}).level == Level.CAUTION
    assert tool.assess({"op": "list"}).level == Level.OK
