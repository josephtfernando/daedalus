"""Tuners: a tune is pending, and only approval is a theme (SPEC §3.6, T9).

*"Tuned values form a pending set, visible in a preview, adopted only on
approve."* Three ways to get that wrong are worth a test each: a tune that moves
the live theme (the approval step performed by nobody); a pending set that does
not survive a restart (the server that restarts between "tune" and "approve"
loses the proposal silently); and a preview that is not the page (a swatch of
colours is not what *seeing* means).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daedalus.genui.server import COOKIE, LocalServer
from daedalus.genui.theming import MOODS, TOKEN_NAMES
from daedalus.genui.tuners import APPROVED_FILE, DRAFTS_DIR, TuneError, Tuners

TOKEN = "a-token-the-owner-never-sees"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def tuners(home: Path) -> Tuners:
    return Tuners(home)


@pytest.fixture
def server(home: Path) -> LocalServer:
    from daedalus.calendar.store import CalendarStore

    return LocalServer(
        home,
        token=TOKEN,
        port=0,
        calendar=CalendarStore(home / "calendar"),
        tuners=Tuners(home),
    )


def _accent(page_or_html: object) -> str:
    text = page_or_html if isinstance(page_or_html, str) else page_or_html.text  # type: ignore[attr-defined]
    for line in text.splitlines():
        if "--d-accent:" in line:
            return line.split(":", 1)[1].rstrip(";").strip()
    raise AssertionError("the page carries no accent token")


# -- a tune is pending, and the live theme does not move ---------------------


def test_a_tune_writes_a_draft_and_the_live_theme_does_not_move(
    server: LocalServer, tuners: Tuners, home: Path
) -> None:
    before = tuners.theme()
    page_before = _accent(server.get("/dashboard"))

    draft = tuners.tune({"accent": "#ff8800"})

    # A tune is not an adoption: no approved file appears, and the theme stays.
    assert not (home / "genui" / APPROVED_FILE).exists(), "a tune is not an adoption"
    assert tuners.theme() == before
    assert [d.values for d in tuners.pending()] == [{"accent": "#ff8800"}]

    # Nor does the dashboard change — only the preview shows the pending set.
    assert _accent(server.get("/dashboard")) == page_before
    assert _accent(server.get(f"/draft/{draft.id}")) == "#ff8800", "the preview shows it"


# -- approval is the only write ---------------------------------------------


def test_approval_adopts_a_complete_theme_over_the_mood_and_previous_adoptions(
    server: LocalServer, tuners: Tuners
) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    tuners.approve(draft.id)

    assert tuners.theme().accent == "#ff8800"
    assert _accent(server.get("/dashboard")) == "#ff8800"
    assert tuners.pending() == []

    # A partial adoption must not leave a hole for a default to fill: every token
    # the sheet reads is supplied, and the mood supplies what was never tuned.
    theme = tuners.theme()
    assert all(getattr(theme, name) for name in TOKEN_NAMES)
    assert theme.bg == MOODS["calm"]["bg"]

    # A second adoption keeps the first: the approved set is merged, not replaced.
    second = tuners.tune({"size": "18px"})
    tuners.approve(second.id)
    merged = tuners.theme()
    assert merged.accent == "#ff8800" and merged.size == "18px"

    # One draft may overwrite another's token — the last yes is the answer.
    tuners.tune({"accent": "#00aa55"})
    tuners.tune({"accent": "#ff0000"})
    for pending in tuners.pending():
        tuners.approve(pending.id)
    assert tuners.theme().accent == "#ff0000"

    # Discarding leaves the theme exactly as it was.
    before = tuners.theme()
    denied = tuners.tune({"accent": "#123456"})
    assert tuners.deny(denied.id) is True
    assert tuners.theme() == before
    assert tuners.pending() == []


# -- the pending set and the adopted set are files --------------------------


def test_a_pending_draft_and_an_adopted_theme_survive_a_restart(
    tuners: Tuners, home: Path
) -> None:
    draft = tuners.tune({"accent": "#ff8800", "size": "17px"}, note="bigger")

    # If the draft lived in memory, a restart between tune and approve would
    # throw the proposal away and the owner would never learn.
    reopened = Tuners(home)
    found = reopened.pending()
    assert [d.id for d in found] == [draft.id]
    assert found[0].values == {"accent": "#ff8800", "size": "17px"}
    assert found[0].note == "bigger"
    assert reopened.draft(draft.id) is not None

    # A draft is a file, so "never mind" is deleting it.
    (home / "genui" / DRAFTS_DIR / f"{draft.id}.toml").unlink()
    assert Tuners(home).pending() == []

    # And an adopted theme is the approved file, so it is there after a restart.
    tuners.tune({"accent": "#ff8800"})
    tuners.approve(tuners.pending()[0].id)
    assert Tuners(home).theme().accent == "#ff8800"


# -- the door ---------------------------------------------------------------


def test_the_door_refuses_what_could_not_become_a_theme(tuners: Tuners) -> None:
    # A typo'd token name, refused with the name it did not recognise.
    with pytest.raises(TuneError) as unknown:
        tuners.tune({"acccent": "#ff8800"})
    assert "unknown token" in str(unknown.value)
    assert tuners.pending() == []

    # Values that could not render — an unknown colour word, a script URL, a
    # length without units, a font that fetches something.
    for values in (
        {"accent": "red"},
        {"accent": "javascript:alert(1)"},
        {"size": "big"},
        {"font": "url(x)"},
    ):
        with pytest.raises(TuneError):
            tuners.tune(values)
        assert tuners.pending() == []

    # And a tune that changes nothing is not a tune.
    with pytest.raises(TuneError):
        tuners.tune({})


def test_a_file_the_loader_cannot_believe_is_refused_when_it_is_read(
    tuners: Tuners, home: Path
) -> None:
    """`approved.toml` is the owner's file; a hand-edit gets the same door."""
    tuners.tune({"accent": "#ff8800"})
    tuners.approve(tuners.pending()[0].id)
    approved = home / "genui" / APPROVED_FILE

    approved.write_text(
        approved.read_text(encoding="utf-8").replace("#ff8800", "red"), encoding="utf-8"
    )
    with pytest.raises(TuneError) as bad_value:
        tuners.theme()
    assert "approved.toml" in str(bad_value.value)

    approved.write_text("[tokens]\nacccent = '#ff8800'\n", encoding="utf-8")
    with pytest.raises(TuneError):
        tuners.theme()

    # A draft that is not even TOML is refused by name, not silently skipped.
    (home / "genui" / DRAFTS_DIR / "broken.toml").write_text(
        "this is not toml(", encoding="utf-8"
    )
    with pytest.raises(TuneError) as broken:
        tuners.pending()
    assert "broken.toml" in str(broken.value)


# -- the preview is the page -------------------------------------------------


def test_the_preview_is_the_real_page_and_only_a_session_is_offered_the_choice(
    server: LocalServer, tuners: Tuners
) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    # Not a swatch: the dashboard, rendered with the pending set.
    preview = server.get(f"/draft/{draft.id}")
    assert preview.status == 200
    assert "calendar__day" in preview.text, "it is the dashboard, rendered"
    assert "#ff8800" in preview.text

    # The choices sit under it only when there is a session to act with.
    invited = server.get(f"/draft/{draft.id}", cookie=f"{COOKIE}={TOKEN}")
    assert "Adopt this theme" in invited.text
    assert "Discard" in invited.text
    assert "calendar.delete" not in invited.text, "a preview proposes a theme, not a delete"

    # An unknown draft is a 404, and an id is matched before it is joined.
    assert server.get("/draft/01NOPE").status == 404
    assert server.get("/draft/../../config").status in (400, 404)


# -- over HTTP: a tune -----------------------------------------------------


def test_a_tune_over_http_is_pending_and_says_where_to_look(
    home: Path, server: LocalServer, tuners: Tuners
) -> None:
    body = json.dumps({"values": {"accent": "#ff8800"}, "note": "warmer"}).encode()

    response = server.post("/tune", body, token=TOKEN, content_type="application/json")

    assert response.status == 201
    payload = json.loads(response.text)
    assert payload["status"] == "pending"
    assert tuners.theme().accent != "#ff8800", "a tune is not an adoption"
    assert f"/draft/{payload['id']}" in payload["message"]

    # A form cannot nest, so its fields are parsed token by token.
    form = server.post(
        "/tune",
        b"accent=%23ff8800&note=warmer",
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )
    assert form.status == 201
    assert {"accent": "#ff8800"} in [d.values for d in tuners.pending()]

    # A typo'd token in a form is refused rather than becoming a pending value.
    typo = server.post(
        "/tune",
        b"acccent=%23ff8800",
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )
    assert typo.status == 400
    assert "unknown token" in typo.text

    # Writes need the token.
    unauthenticated = json.dumps({"values": {"accent": "#ff8800"}}).encode()
    assert server.post("/tune", unauthenticated).status == 401
    assert server.post("/tune", unauthenticated, token="wrong").status == 401

    # A server started without tuners answers as unbuilt, not as broken.
    from daedalus.calendar.store import CalendarStore

    bare = LocalServer(home, token=TOKEN, port=0, calendar=CalendarStore(home / "calendar"))
    for route in ("/tune", "/approve"):
        unbuilt = bare.post(route, body, token=TOKEN, content_type="application/json")
        assert unbuilt.status == 501
        assert "not built yet" in unbuilt.text


# -- over HTTP: approve, discard, and the whole round trip ------------------


def test_approve_and_discard_over_http_are_spelled_and_the_round_trip_holds(
    server: LocalServer, tuners: Tuners
) -> None:
    before = _accent(server.get("/dashboard"))

    tuned = server.post(
        "/tune",
        b"accent=%23ff8800",
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )
    draft = tuners.pending()[0]
    preview = server.get(f"/draft/{draft.id}", cookie=f"{COOKIE}={TOKEN}")
    assert _accent(preview.text) == "#ff8800"

    adopted = server.post(
        "/approve",
        f"id={draft.id}&decision=approve".encode(),
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )

    assert tuned.status == 201 and adopted.status == 200
    assert _accent(server.get("/dashboard")) == "#ff8800"
    assert _accent(server.get("/dashboard")) != before
    assert Tuners(server.home).theme().accent == "#ff8800"

    # A discard is the other decision, and removes the draft.
    denied = tuners.tune({"accent": "#00aa55"})
    discard = server.post(
        "/approve",
        json.dumps({"id": denied.id, "decision": "deny"}).encode(),
        token=TOKEN,
        content_type="application/json",
    )
    assert discard.status == 200
    assert tuners.pending() == []
    assert tuners.theme().accent == "#ff8800", "a discard leaves the theme as it was"

    # Approve and discard must never be one default apart.
    undecided = tuners.tune({"accent": "#ff0000"})
    refused = server.post(
        "/approve",
        json.dumps({"id": undecided.id}).encode(),
        token=TOKEN,
        content_type="application/json",
    )
    assert refused.status == 400
    assert tuners.pending(), "nothing happened to the draft"
