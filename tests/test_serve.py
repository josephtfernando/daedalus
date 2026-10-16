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


def test_the_bind_address_is_loopback_and_is_a_constant(tmp_path: Path) -> None:
    assert LocalServer(tmp_path, port=0).host == LOOPBACK
    assert ipaddress.ip_address(LOOPBACK).is_loopback
    assert LOOPBACK == "127.0.0.1"


def test_it_comes_up_on_a_loopback_address_and_not_a_wildcard(server: LocalServer) -> None:
    """Asserted on the socket that is actually listening, not on the intent."""
    server.start()
    try:
        assert ipaddress.ip_address(server.host).is_loopback
        assert server.port != 0, "an ephemeral port was never resolved"
    finally:
        server.stop()


def test_a_wildcard_bind_is_not_reachable_from_the_code(tmp_path: Path) -> None:
    """``0.0.0.0`` is not a parameter: the only bind this server can make is the loop."""
    with pytest.raises(TypeError):
        LocalServer(tmp_path, host="0.0.0.0")  # type: ignore[call-arg]


# -- serving the owner's artifacts ------------------------------------------


def test_a_generated_artifact_is_served(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)
    (app.artifacts / "day.html").write_text("<h1>Tuesday</h1>", encoding="utf-8")

    response = app.get("/day.html")

    assert response.status == 200
    assert response.content_type.startswith("text/html")
    assert "Tuesday" in response.text


def test_the_front_page_is_the_index_when_there_is_one(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)
    (app.artifacts / "index.html").write_text("<h1>now</h1>", encoding="utf-8")
    (app.artifacts / "week.html").write_text("<h1>week</h1>", encoding="utf-8")

    assert "now" in app.get("/").text


def test_the_front_page_lists_what_exists_when_there_is_no_index(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)
    (app.artifacts / "week.html").write_text("<h1>week</h1>", encoding="utf-8")

    page = app.get("/").text

    assert "week.html" in page and 'href="/week.html"' in page


def test_the_health_endpoint_says_the_server_is_real(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)

    payload = json.loads(app.get("/healthz").text)

    assert payload["ok"] is True and payload["artifacts"] == 0


def test_nothing_generated_is_an_empty_page_not_an_error(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)

    response = app.get("/")

    assert response.status == 200
    assert "Nothing generated yet" in response.text


def test_an_unknown_artifact_is_a_404(tmp_path: Path) -> None:
    app = LocalServer(tmp_path, port=0)

    assert app.get("/nowhere.html").status == 404


# -- a request cannot climb out of the artifact directory -------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../config.toml",
        "/..%2fconfig.toml",
        "/%2e%2e/%2e%2e/.env",
        "/sub/../../config.toml",
        "/.env",
        "/../../../../etc/passwd",
    ],
)
def test_a_path_that_leaves_the_artifact_directory_is_refused(tmp_path: Path, path: str) -> None:
    app = LocalServer(tmp_path, port=0)
    (app.home / "config.toml").write_text("[owner]\nname = 'secret'", encoding="utf-8")

    response = app.get(path)

    assert response.status in (400, 404)
    assert "secret" not in response.text


def test_a_symlink_out_of_the_directory_is_refused(tmp_path: Path) -> None:
    """A resolved-parent check, not a string check — symlinks are not a loophole."""
    app = LocalServer(tmp_path, port=0)
    outside = tmp_path / "secret.txt"
    outside.write_text("private", encoding="utf-8")
    (app.artifacts / "escape.html").symlink_to(outside)

    response = app.get("/escape.html")

    assert response.status == 400
    assert "private" not in response.text


# -- no anonymous controls --------------------------------------------------


def test_a_write_without_the_token_is_refused(server: LocalServer) -> None:
    assert server.post("/tune", b"{}").status == 401


def test_a_write_with_the_wrong_token_is_refused(server: LocalServer) -> None:
    assert server.post("/tune", b"{}", token="not-the-token").status == 401


def test_a_write_with_the_token_gets_past_the_gate(server: LocalServer) -> None:
    """501 not-built means the gate opened; 401 would mean it did not."""
    response = server.post("/tune", b"{}", token=TOKEN)

    assert response.status == 501
    assert "not built yet" in response.text


def test_a_server_with_no_token_accepts_no_writes_at_all(tmp_path: Path) -> None:
    """Unset must fail closed. An empty token is not a wildcard."""
    app = LocalServer(tmp_path, port=0)
    assert app.token == ""

    assert app.post("/tune", b"{}").status == 401
    assert app.post("/tune", b"{}", token="").status == 401
    assert app.post("/tune", b"{}", token="anything").status == 401


def test_an_unknown_control_is_a_404_but_only_once_authorised(server: LocalServer) -> None:
    assert server.post("/nonsense", token=TOKEN).status == 404


def test_reads_are_open_because_that_is_what_loopback_means(server: LocalServer) -> None:
    assert server.get("/healthz").status == 200


# -- the routing table tells the truth about what is missing ----------------


@pytest.mark.parametrize("route", ["/dashboard", "/draft"])
def test_an_unbuilt_read_says_so_rather_than_404(tmp_path: Path, route: str) -> None:
    app = LocalServer(tmp_path, port=0)

    response = app.get(route)

    assert response.status == 501
    assert "not built yet" in response.text


@pytest.mark.parametrize("route", ["/tune", "/approve", "/action"])
def test_an_unbuilt_control_says_so_rather_than_404(server: LocalServer, route: str) -> None:
    response = server.post(route, b"{}", token=TOKEN)

    assert response.status == 501
    assert "not built yet" in response.text


# -- the token --------------------------------------------------------------


def test_the_control_token_is_minted_once_and_kept_encrypted(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)

    first = control_token(store)
    second = control_token(store)

    assert first == second
    assert len(first) > 20
    blob = (store.dir / "dashboard-token.enc").read_bytes()
    assert first.encode() not in blob, "the token is readable at rest"


def test_the_token_is_not_the_empty_string(tmp_path: Path) -> None:
    assert control_token(SecretStore(tmp_path)) != ""


# -- over a real socket -----------------------------------------------------


def test_a_real_request_round_trips_and_carries_the_hardening_headers(tmp_path: Path) -> None:
    """The routing table is unit-tested above; this proves the wiring is live."""
    app = LocalServer(tmp_path, token=TOKEN, port=0)
    (app.artifacts / "day.html").write_text("<h1>Tuesday</h1>", encoding="utf-8")
    app.start()
    try:
        with urllib.request.urlopen(f"{app.url}/day.html", timeout=5) as response:
            assert response.status == 200
            assert "Tuesday" in response.read().decode("utf-8")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Cache-Control"] == "no-store"

        request = urllib.request.Request(
            f"{app.url}/tune", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == 401

        request.add_header("X-Daedalus-Token", TOKEN)
        with pytest.raises(urllib.error.HTTPError) as gated:
            urllib.request.urlopen(request, timeout=5)
        assert gated.value.code == 501
    finally:
        app.stop()


def test_stopping_twice_is_harmless(server: LocalServer) -> None:
    server.start()
    server.stop()
    server.stop()


def test_serving_twice_does_not_bind_a_second_time(server: LocalServer) -> None:
    first = server.start()
    second = server.start()
    try:
        assert first == second
    finally:
        server.stop()
