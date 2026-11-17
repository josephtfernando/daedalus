"""The published artifact (T25, T34) and the Taildrop leg (T15).

One mechanism, tested from both sides: the registry's gate (one-time spend,
expiry, takedown, token) and the server's route that enforces it, then the
tool that reaches both — including the containment clause that publishing
cannot read where the files primitive cannot, and the push that names a
device rather than copying into the air. (The dashboard publish's own tests —
the Funnel refusal in `genui/publish.py` — live in test_publish.py.)
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


def test_a_publication_is_one_time_token_gated_expiring_and_takeable_down(
    pubs: Publications, tmp_path: Path
) -> None:
    import time

    # one-time by default: the first admit spends the link, the second refuses
    direct = pubs.publish("spent.txt")
    assert direct.max_fetches == 1
    ok, reason, _ = pubs.admit("spent.txt", direct.token)
    assert (ok, reason) == (True, "")
    spent, spent_reason, _ = pubs.admit("spent.txt", direct.token)
    assert not spent and "already fetched" in spent_reason

    staged(tmp_path)
    srv = server(tmp_path, pubs)

    # a wrong token refuses and does not spend; the right one serves, once
    pub = pubs.publish("brief.txt")
    wrong = srv.get("/pub/brief.txt?t=not-this-one")
    assert wrong.status == 403
    assert pubs.get("brief.txt").fetches == 0, "a wrong token must not spend the link"

    first = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert first.status == 200 and first.body == b"payload"
    assert dict(first.headers)["Cache-Control"] == "no-store", (
        "a one-time link served from browser cache was never one-time"
    )

    second = srv.get(f"/pub/brief.txt?t={pub.token}")
    assert second.status == 410, "the second open finds the gate closed, honestly named"

    # expiry is a read-path fact, not a sweep's
    expiring = pubs.publish("expired.txt", lifetime_s=0.05)
    time.sleep(0.06)  # the lifetime runs out while nobody is sweeping
    (tmp_path / "home" / "serve" / "expired.txt").write_bytes(b"payload")
    expired = srv.get(f"/pub/expired.txt?t={expiring.token}")
    assert expired.status == 410 and "expired" in expired.text

    # takedown marks the record dead, is idempotent, and the route answers 410
    live = pubs.publish("live.txt")
    (tmp_path / "home" / "serve" / "live.txt").write_bytes(b"payload")
    assert pubs.takedown("live.txt") is True
    assert pubs.takedown("live.txt") is False, "a takedown is idempotent — not a second act"
    assert pubs.get("live.txt").taken_down
    dead = srv.get(f"/pub/live.txt?t={live.token}")
    assert dead.status == 410 and "taken down" in dead.text

    # names are one plain file name, and a live publication is not replaced
    for bad in ("../escape", "a/b", r"..\win"):
        with pytest.raises(ValueError):
            pubs.publish(bad)
    pubs.publish("slot.txt")
    with pytest.raises(ValueError):
        pubs.publish("slot.txt"), "an alive publication is not replaced by a second link"
    pubs.takedown("slot.txt")
    pubs.publish("slot.txt")  # a dead one may be replaced

    # a server with no registry answers as if nothing, not as if closed
    bare = LocalServer(tmp_path / "home", artifacts=tmp_path / "home" / "ui")
    assert bare.get("/pub/anything?t=x").status == 404, (
        "a server without publications answers as if nothing, not as if closed"
    )


async def test_the_tool_serves_within_the_files_reach_and_never_in_front_of_a_funnel(
    runtime, monkeypatch
) -> None:
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
    assert (runtime.settings.home / "serve" / allowed.name).is_file(), (
        "the publication's bytes live in serve/, not in ui/"
    )

    blocked = runtime.settings.home / "docs" / "private" / "note.txt"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("x")
    refused = await tool.run({"op": "serve", "path": str(blocked)}, runtime)
    assert not refused.ok and "protected" in refused.output

    big = runtime.settings.home / "big.bin"
    big.write_bytes(b"\0" * (65 * 1024 * 1024))
    oversized = await tool.run({"op": "serve", "path": str(big)}, runtime)
    assert not oversized.ok and "publication cap" in oversized.output

    # Funnel on is a refusal, and refusal precedes the move; unknown is one too
    src = runtime.settings.home / "small.txt"
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


async def test_the_link_is_a_real_url_with_its_token_and_a_takedown_deletes_the_bytes(
    runtime, monkeypatch
) -> None:
    from daedalus.approvals.classify import Level
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
    assert after.ok and not (runtime.settings.home / "serve" / src.name).exists(), (
        "takedown deletes the bytes — a dead link with live bytes is a leak in waiting"
    )

    # the public acts are cautioned; reading the list is not
    assert tool.risk_for({"op": "serve"}) is not None
    assert tool.assess({"op": "serve"}).level == Level.CAUTION
    assert tool.assess({"op": "push"}).level == Level.CAUTION
    assert tool.assess({"op": "list"}).level == Level.OK


async def test_serving_a_file_that_already_lives_in_serve_borrows_it_and_leaves_it(
    runtime, monkeypatch
) -> None:
    """The owner asked for a file *in* serve/ — so publishing one must not copy
    it onto itself, and taking the link down must not delete their only copy.

    Lived as a traceback: `shutil.SameFileError` when the agent wrote its own
    artifact into serve/ and then published it, and a `unlink` waiting to eat
    the owner's file on takedown.
    """
    from daedalus.tools import publish as publish_module

    monkeypatch.setattr(publish_module, "funnel_on", lambda: False)
    tool = PushTool()

    serve_dir = runtime.settings.home / "serve"
    serve_dir.mkdir(parents=True, exist_ok=True)
    mine = serve_dir / "catalog.md"
    mine.write_text("# the owner's own file\n")

    served = await tool.run({"op": "serve", "path": str(mine), "fetches": 2}, runtime)
    assert served.ok, served.output
    assert mine.read_text() == "# the owner's own file\n", "the copy overwrote its own source"
    record = runtime.publications.get("catalog.md")
    assert record is not None and record.owns_bytes is False

    gone = await tool.run({"op": "takedown", "name": "catalog.md"}, runtime)
    assert gone.ok and "untouched" in gone.output
    assert mine.is_file(), "a takedown deleted a file the publication never made"

    # a copy the tool did make is still deleted — the takedown's own guarantee
    elsewhere = runtime.settings.home / "notes.md"
    elsewhere.write_text("mine too")
    assert (await tool.run({"op": "serve", "path": str(elsewhere)}, runtime)).ok
    await tool.run({"op": "takedown", "name": "notes.md"}, runtime)
    assert not (serve_dir / "notes.md").exists()


async def test_push_names_a_peer_and_refuses_the_air(runtime, monkeypatch) -> None:
    import threading

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

    # the tailscale subprocess runs off the event loop, so a slow call cannot
    # freeze typing, heartbeats, or every turn
    seen: list[bool] = []

    def targets() -> list[str]:
        seen.append(threading.current_thread() is threading.main_thread())
        return ["phone"]

    monkeypatch.setattr(publish_module, "_targets", targets)
    again = await PushTool().run({"op": "peers"}, runtime)
    assert again.ok and "phone" in again.output
    assert seen == [False], "the subprocess ran on the loop's thread"
