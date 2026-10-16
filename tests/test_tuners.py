"""Tuners: a tune is pending, and only approval is a theme (SPEC §3.6, T9).

*"Tuned values form a pending set, visible in a preview, adopted only on
approve."* Three ways to get that wrong are worth a test each:

- **A tune that moves the live theme.** The owner asked to *see* a change; a
  tune that rewrites the theme behind their back is the approval step performed
  by nobody, on nobody's behalf.
- **A pending set that does not survive a restart.** If the draft lives in
  memory, the server that restarts between "tune" and "approve" throws the
  proposal away — and the owner never learns their tune was lost.
- **A preview that is not the page.** A swatch of colours is not what *seeing*
  means; the preview is the real dashboard rendered with the pending set, and
  the choices sit under it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.genui.dashboard import calendar_page
from daedalus.genui.server import COOKIE, LocalServer
from daedalus.genui.theming import MOODS, TOKEN_NAMES
from daedalus.genui.tuners import APPROVED_FILE, TuneError, Tuners

TOKEN = "a-token-the-owner-never-sees"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def tuners(home: Path) -> Tuners:
    return Tuners(home)


@pytest.fixture
def server(home: Path) -> LocalServer:
    from daedalus.genui.actions import Controls  # noqa: F401 — not attached here

    from daedalus.calendar.store import CalendarStore
    from daedalus.genui.tuners import Tuners as T

    return LocalServer(
        home,
        token=TOKEN,
        port=0,
        calendar=CalendarStore(home / "calendar"),
        tuners=T(home),
    )


def _accent(page_or_html: object) -> str:
    text = page_or_html if isinstance(page_or_html, str) else page_or_html.text  # type: ignore[attr-defined]
    for line in text.splitlines():
        if "--d-accent:" in line:
            return line.split(":", 1)[1].rstrip(";").strip()
    raise AssertionError("the page carries no accent token")


# -- a tune is pending, and the live theme does not move ---------------------


def test_a_tune_writes_a_draft_and_nothing_else(tuners: Tuners, home: Path) -> None:
    tuners.tune({"accent": "#ff8800"})

    assert not (home / "genui" / APPROVED_FILE).exists(), "a tune is not an adoption"
    assert [d.values for d in tuners.pending()] == [{"accent": "#ff8800"}]


def test_the_live_theme_is_unchanged_until_approval(tuners: Tuners) -> None:
    before = tuners.theme()

    tuners.tune({"accent": "#ff8800"})

    assert tuners.theme() == before


def test_the_dashboard_does_not_change_until_approval(
    server: LocalServer, tuners: Tuners
) -> None:
    before = _accent(server.get("/dashboard"))

    draft = tuners.tune({"accent": "#ff8800"})

    assert _accent(server.get("/dashboard")) == before
    assert _accent(server.get(f"/draft/{draft.id}")) == "#ff8800", "the preview shows it"


def test_approval_adopts_and_the_page_follows(
    server: LocalServer, tuners: Tuners
) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    tuners.approve(draft.id)

    assert tuners.theme().accent == "#ff8800"
    assert _accent(server.get("/dashboard")) == "#ff8800"


def test_approval_clears_the_pending_set(tuners: Tuners) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    tuners.approve(draft.id)

    assert tuners.pending() == []


def test_discarding_leaves_the_theme_as_it_was(tuners: Tuners) -> None:
    before = tuners.theme()
    draft = tuners.tune({"accent": "#ff8800"})

    assert tuners.deny(draft.id) is True

    assert tuners.theme() == before
    assert tuners.pending() == []


# -- the pending set is files ------------------------------------------------


def test_a_pending_set_survives_a_restart(tuners: Tuners, home: Path) -> None:
    draft = tuners.tune({"accent": "#ff8800", "size": "17px"}, note="bigger")

    reopened = Tuners(home)
    found = reopened.pending()

    assert [d.id for d in found] == [draft.id]
    assert found[0].values == {"accent": "#ff8800", "size": "17px"}
    assert found[0].note == "bigger"
    assert reopened.draft(draft.id) is not None


def test_a_draft_is_a_file_so_deleting_it_is_discarding(
    tuners: Tuners, home: Path
) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    (home / "genui" / "drafts" / f"{draft.id}.toml").unlink()

    assert Tuners(home).pending() == []


def test_an_adopted_theme_survives_a_restart(tuners: Tuners, home: Path) -> None:
    tuners.tune({"accent": "#ff8800"})
    tuners.approve(tuners.pending()[0].id)

    reopened = Tuners(home)

    assert reopened.theme().accent == "#ff8800"


def test_a_second_adoption_keeps_the_first(tuners: Tuners) -> None:
    first = tuners.tune({"accent": "#ff8800"})
    second = tuners.tune({"size": "18px"})

    tuners.approve(first.id)
    tuners.approve(second.id)

    theme = tuners.theme()
    assert theme.accent == "#ff8800" and theme.size == "18px"


def test_a_second_tune_of_the_same_token_wins_when_adopted(tuners: Tuners) -> None:
    """One draft may overwrite another's token — the last yes is the answer."""
    tuners.tune({"accent": "#ff8800"})
    tuners.tune({"accent": "#00aa55"})
    for draft in tuners.pending():
        tuners.approve(draft.id)

    assert tuners.theme().accent == "#00aa55"


# -- the door ----------------------------------------------------------------


def test_an_unknown_token_is_refused(tuners: Tuners) -> None:
    with pytest.raises(TuneError) as refused:
        tuners.tune({"acccent": "#ff8800"})

    assert "unknown token" in str(refused.value)
    assert tuners.pending() == []


@pytest.mark.parametrize(
    "values", [{"accent": "red"}, {"accent": "javascript:alert(1)"}, {"size": "big"}, {"font": "url(x)"}]
)
def test_a_value_that_could_not_render_is_refused_at_the_door(
    tuners: Tuners, values: dict
) -> None:
    with pytest.raises(TuneError):
        tuners.tune(values)

    assert tuners.pending() == []


def test_an_empty_tune_is_refused(tuners: Tuners) -> None:
    with pytest.raises(TuneError):
        tuners.tune({})


def test_a_draft_the_loader_cannot_believe_is_refused(tuners: Tuners, home: Path) -> None:
    tuners.tune({"accent": "#ff8800"})
    path = home / "genui" / "drafts"
    (path / "broken.toml").write_text("this is not toml(", encoding="utf-8")

    with pytest.raises(TuneError) as refused:
        tuners.pending()

    assert "broken.toml" in str(refused.value)


def test_an_adopted_theme_is_checked_again_when_it_is_read(
    tuners: Tuners, home: Path
) -> None:
    """`approved.toml` is the owner's file; a hand-edit gets the same door."""
    tuners.tune({"accent": "#ff8800"})
    tuners.approve(tuners.pending()[0].id)
    approved = home / "genui" / APPROVED_FILE
    approved.write_text(
        approved.read_text(encoding="utf-8").replace("#ff8800", "red"), encoding="utf-8"
    )

    with pytest.raises(TuneError) as refused:
        tuners.theme()

    assert "approved.toml" in str(refused.value)


def test_a_token_name_nobody_declared_in_the_approved_file_is_refused(
    tuners: Tuners, home: Path
) -> None:
    approved = home / "genui" / APPROVED_FILE
    approved.parent.mkdir(parents=True, exist_ok=True)
    approved.write_text("[tokens]\nacccent = '#ff8800'\n", encoding="utf-8")

    with pytest.raises(TuneError):
        tuners.theme()


def test_an_adopted_theme_covers_every_token_the_sheet_reads(tuners: Tuners) -> None:
    """A partial adoption must not leave a hole for a default to fill."""
    tuners.tune({"accent": "#ff8800"})
    tuners.approve(tuners.pending()[0].id)

    theme = tuners.theme()

    assert all(getattr(theme, name) for name in TOKEN_NAMES)
    assert theme.accent == "#ff8800"


def test_the_mood_still_supplies_what_was_never_tuned(tuners: Tuners) -> None:
    tuners.approve(tuners.tune({"accent": "#ff8800"}).id)

    assert tuners.theme().bg == MOODS["calm"]["bg"]


# -- the preview -------------------------------------------------------------


def test_the_preview_is_the_real_page_not_a_swatch(
    server: LocalServer, tuners: Tuners
) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    response = server.get(f"/draft/{draft.id}")

    assert response.status == 200
    assert "calendar__day" in response.text, "it is the dashboard, rendered"
    assert "#ff8800" in response.text


def test_the_preview_offers_the_choice_when_there_is_a_session(
    server: LocalServer, tuners: Tuners
) -> None:
    draft = tuners.tune({"accent": "#ff8800"})

    response = server.get(f"/draft/{draft.id}", cookie=f"{COOKIE}={TOKEN}")

    assert "Adopt this theme" in response.text
    assert "Discard" in response.text
    assert "calendar.delete" not in response.text, "a preview proposes a theme, not a delete"


def test_an_unknown_draft_is_a_404(server: LocalServer) -> None:
    response = server.get("/draft/01NOPE")

    assert response.status == 404


def test_a_draft_id_is_matched_before_it_is_joined(server: LocalServer) -> None:
    assert server.get("/draft/../../config").status in (400, 404)


# -- over HTTP ---------------------------------------------------------------


def test_a_tune_over_http_is_pending_and_answers_where_to_look(
    server: LocalServer, tuners: Tuners
) -> None:
    body = json.dumps({"values": {"accent": "#ff8800"}, "note": "warmer"}).encode()

    response = server.post("/tune", body, token=TOKEN, content_type="application/json")

    assert response.status == 201
    payload = json.loads(response.text)
    assert payload["status"] == "pending"
    assert tuners.theme().accent != "#ff8800", "a tune is not an adoption"
    assert f"/draft/{payload['id']}" in payload["message"]


def test_a_tune_from_a_form_is_parsed_token_by_token(
    server: LocalServer, tuners: Tuners
) -> None:
    response = server.post(
        "/tune",
        b"accent=%23ff8800&note=warmer",
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status == 201
    assert [d.values for d in tuners.pending()] == [{"accent": "#ff8800"}]


def test_a_form_tune_with_a_typo_d_token_is_refused(server: LocalServer) -> None:
    response = server.post(
        "/tune",
        b"acccent=%23ff8800",
        token=TOKEN,
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status == 400
    assert "unknown token" in response.text


def test_an_approve_over_http_adopts_and_answers(server: LocalServer, tuners: Tuners) -> None:
    draft = tuners.tune({"accent": "#ff8800"})
    body = json.dumps({"id": draft.id, "decision": "approve"}).encode()

    response = server.post("/approve", body, token=TOKEN, content_type="application/json")

    assert response.status == 200
    assert json.loads(response.text)["status"] == "adopted"
    assert tuners.theme().accent == "#ff8800"
    assert tuners.pending() == []


def test_a_discard_over_http_removes_the_draft(server: LocalServer, tuners: Tuners) -> None:
    draft = tuners.tune({"accent": "#ff8800"})
    body = json.dumps({"id": draft.id, "decision": "deny"}).encode()

    response = server.post("/approve", body, token=TOKEN, content_type="application/json")

    assert response.status == 200
    assert tuners.pending() == []
    assert tuners.theme().accent != "#ff8800"


def test_an_approval_that_names_no_decision_is_refused(
    server: LocalServer, tuners: Tuners
) -> None:
    """Approve and discard must never be one default apart."""
    draft = tuners.tune({"accent": "#ff8800"})

    response = server.post(
        "/approve",
        json.dumps({"id": draft.id}).encode(),
        token=TOKEN,
        content_type="application/json",
    )

    assert response.status == 400
    assert tuners.pending(), "nothing happened to the draft"


def test_a_tune_without_the_token_is_refused(server: LocalServer) -> None:
    body = json.dumps({"values": {"accent": "#ff8800"}}).encode()

    assert server.post("/tune", body).status == 401
    assert server.post("/tune", body, token="wrong").status == 401


def test_a_server_without_tuners_says_so(home: Path) -> None:
    """A server started without the tuners answers as unbuilt, not as broken."""
    from daedalus.calendar.store import CalendarStore

    app = LocalServer(home, token=TOKEN, port=0, calendar=CalendarStore(home / "calendar"))
    body = json.dumps({"values": {"accent": "#ff8800"}}).encode()

    for route in ("/tune", "/approve"):
        response = app.post(route, body, token=TOKEN, content_type="application/json")
        assert response.status == 501
        assert "not built yet" in response.text


# -- the whole round trip, as the owner would meet it ------------------------


def test_the_round_trip_from_the_browser(server: LocalServer, tuners: Tuners) -> None:
    """Tune → see it → adopt it → the dashboard is different, and stays so."""
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
