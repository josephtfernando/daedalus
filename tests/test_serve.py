"""The loopback server: private by construction, and no anonymous controls.

Two failure modes are worth a test each, because both are the kind that look
fine right up until they are not. A server that binds anything other than the
loopback is the entire security model gone, and a server that treats *unset* as
*open* is an unauthenticated remote control for the owner's calendar. Neither is
observable by reading the happy path, so both are asserted here.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from daedalus.genui.server import LOOPBACK, LocalServer, control_token
from daedalus.resources.secrets import SecretStore

TOKEN = "a-token-the-owner-never-sees"


@pytest.fixture
def server(tmp_path: Path) -> LocalServer:
    return LocalServer(tmp_path, token=TOKEN, port=0)


# -- private by construction ------------------------------------------------


def test_the_bind_is_loopback_and_the_server_comes_up_and_down_cleanly(
    tmp_path: Path,
) -> None:
    app = LocalServer(tmp_path, port=0)
    assert app.host == LOOPBACK
    assert ipaddress.ip_address(LOOPBACK).is_loopback

    # ``0.0.0.0`` is not a parameter: the only bind this server can make is the loop.
    with pytest.raises(TypeError):
        LocalServer(tmp_path, host="0.0.0.0")  # type: ignore[call-arg]

    # Asserted on the socket that is actually listening, not on the intent.
    first = app.start()
    second = app.start()
    try:
        assert ipaddress.ip_address(app.host).is_loopback
        assert app.port != 0, "an ephemeral port was never resolved"
        assert first == second, "serving twice must not bind a second time"
    finally:
        app.stop()
    app.stop()  # stopping twice is harmless


# -- serving the owner's artifacts -----------------------------------------


def test_artifacts_are_served_and_the_front_page_is_what_exists(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)

    # Nothing generated is an empty page, not an error.
    empty = app.get("/")
    assert empty.status == 200
    assert "Nothing generated yet" in empty.text
    assert app.get("/nowhere.html").status == 404

    payload = json.loads(app.get("/healthz").text)
    assert payload["ok"] is True and payload["artifacts"] == 0

    (app.artifacts / "day.html").write_text("<h1>Tuesday</h1>", encoding="utf-8")
    served = app.get("/day.html")
    assert served.status == 200
    assert served.content_type.startswith("text/html")
    assert "Tuesday" in served.text

    # With no index, the front page lists what exists.
    listing = app.get("/").text
    assert "day.html" in listing and 'href="/day.html"' in listing

    # With one, the index is the front page.
    (app.artifacts / "index.html").write_text("<h1>now</h1>", encoding="utf-8")
    (app.artifacts / "week.html").write_text("<h1>week</h1>", encoding="utf-8")
    assert "now" in app.get("/").text


# -- a request cannot climb out of the artifact directory -------------------


def test_a_request_cannot_climb_out_of_the_artifact_directory(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)
    (app.home / "config.toml").write_text("[owner]\nname = 'secret'", encoding="utf-8")

    for path in (
        "/../config.toml",
        "/..%2fconfig.toml",
        "/%2e%2e/%2e%2e/.env",
        "/sub/../../config.toml",
        "/.env",
        "/../../../../etc/passwd",
    ):
        response = app.get(path)
        assert response.status in (400, 404), path
        assert "secret" not in response.text, path

    # A resolved-parent check, not a string check — symlinks are not a loophole.
    outside = tmp_path / "secret.txt"
    outside.write_text("private", encoding="utf-8")
    (app.artifacts / "escape.html").symlink_to(outside)

    response = app.get("/escape.html")
    assert response.status == 400
    assert "private" not in response.text


# -- no anonymous controls --------------------------------------------------


def test_no_anonymous_controls_and_an_unset_token_fails_closed(
    server: LocalServer, tmp_path: Path
) -> None:
    assert server.post("/tune", b"{}").status == 401
    assert server.post("/tune", b"{}", token="not-the-token").status == 401

    # 501 not-built means the gate opened; 401 would mean it did not.
    passed = server.post("/tune", b"{}", token=TOKEN)
    assert passed.status == 501
    assert "not built yet" in passed.text

    # An unknown control is a 404, but only once authorised.
    assert server.post("/nonsense", token=TOKEN).status == 404

    # Reads are open, because that is what loopback means.
    assert server.get("/healthz").status == 200

    # Unset must fail closed. An empty token is not a wildcard.
    unset = LocalServer(tmp_path, port=0)
    assert unset.token == ""
    assert unset.post("/tune", b"{}").status == 401
    assert unset.post("/tune", b"{}", token="").status == 401
    assert unset.post("/tune", b"{}", token="anything").status == 401


# -- the routing table tells the truth about what is missing ----------------


def test_a_route_that_is_designed_but_unbuilt_says_so_rather_than_404(
    server: LocalServer, tmp_path: Path
) -> None:
    # "not built yet" and "not a thing" must be distinguishable.
    unbuilt_read = LocalServer(tmp_path, port=0).get("/draft")
    assert unbuilt_read.status == 501
    assert "not built yet" in unbuilt_read.text

    for route in ("/tune", "/approve", "/action"):
        response = server.post(route, b"{}", token=TOKEN)
        assert response.status == 501, route
        assert "not built yet" in response.text, route


# -- the token --------------------------------------------------------------


def test_the_control_token_is_minted_once_and_kept_encrypted(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)

    first = control_token(store)
    second = control_token(store)

    assert first == second
    assert first != "", "an empty token would be a wildcard"
    assert len(first) > 20, "the token is a real secret, not a guessable word"
    blob = (store.dir / "dashboard-token.enc").read_bytes()
    assert first.encode() not in blob, "the token is readable at rest"


# -- over a real socket -----------------------------------------------------


def test_a_real_request_round_trips_with_the_hardening_headers(tmp_path: Path) -> None:
    """The routing table is unit-tested above; this proves the wiring is live."""
    app = LocalServer(tmp_path, token=TOKEN, port=0)
    (app.artifacts / "day.html").write_text("<h1>Tuesday</h1>", encoding="utf-8")
    (app.artifacts / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    app.start()
    try:
        with urllib.request.urlopen(f"{app.url}/day.html", timeout=5) as response:
            assert response.status == 200
            assert "Tuesday" in response.read().decode("utf-8")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            # The policy travels as a header as well as a <meta>: a meta policy
            # only applies from where it is parsed, so a header is what covers
            # the first bytes of the document.
            assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")

        # A policy on a PNG is not a policy, it is noise; on a document it is the point.
        with urllib.request.urlopen(f"{app.url}/shot.png", timeout=5) as response:
            assert "Content-Security-Policy" not in response.headers

        request = urllib.request.Request(f"{app.url}/tune", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == 401

        request.add_header("X-Daedalus-Token", TOKEN)
        with pytest.raises(urllib.error.HTTPError) as gated:
            urllib.request.urlopen(request, timeout=5)
        assert gated.value.code == 501
    finally:
        app.stop()
