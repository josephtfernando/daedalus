"""The renderer: one self-contained page, and values that never left the laptop.

The pair that matters most is `test_a_local_only_value_reaches_the_page_and_was_never_in_the_spec`.
SPEC §6 says a `local_only` fact never enters a remote-bound prompt, and the honest
way to keep that is for the fact to be absent from the spec *entirely* — the model
composes `{"bind": "calendar.today.events"}` and a local lookup fills it in. So the
test asserts both halves: the page contains the value, and the spec the model held
does not — before *and* after rendering. The rest is containment: no scripts, no
external anything, and a CSP that makes the browser enforce it rather than asking.
"""

from __future__ import annotations

import json
from html import unescape

import pytest

from daedalus.genui.components import VOCABULARY, render
from daedalus.genui.renderer import CSP, render_fragment, render_page
from daedalus.genui.spec import Binding, Block, Spec, SpecError
from daedalus.genui.theming import tokens_for


def make(raw: object) -> Spec:
    return Spec.parse(raw, vocabulary=VOCABULARY)


LIST_SPEC = {
    "title": "Today",
    "blocks": [
        {"component": "list", "props": {"items": ["a", "b"]}},
        {"component": "stat-card", "props": {"label": "Free", "value": "42 GB"}},
    ],
}


def test_a_rendered_page_is_one_self_contained_document_that_fetches_nothing() -> None:
    page = render_page(make(LIST_SPEC))

    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")
    assert '<html lang="en">' in page
    assert page.count("<style>") == 1
    assert "<title>Today</title>" in page

    # Built for a phone as well as a laptop.
    assert 'name="viewport"' in page
    assert "width=device-width" in page

    # Self-contained is the promise; nothing may reach out for anything.
    for forbidden in ("<script", "<link", "@import", "url(", "http://", "https://", "src="):
        assert forbidden not in page, f"the page contains {forbidden!r}"

    # A test that asserts a page is safe is still a test; the browser is the guarantee.
    assert 'http-equiv="Content-Security-Policy"' in page
    # Attribute-escaped, which the parser decodes before the policy is read —
    # `'none'` arrives as `&#x27;none&#x27;` and means the same thing.
    policy = unescape(page)
    assert "default-src 'none'" in policy
    assert "script-src" not in policy, "a script source was allowed to something"
    assert "form-action 'none'" in policy
    assert CSP in policy

    # A rendered page is the owner's; it should not narrate how it was assembled.
    assert "<!--" not in page

    # An empty spec still makes a valid page, with a fallback title.
    empty = render_page(make({"blocks": []}))
    assert empty.startswith("<!doctype html>")
    assert '<main class="dashboard">' in empty
    assert "Daedalus" in empty, "an untitled page should still have a title"


def test_the_fragment_is_the_page_minus_its_shell_and_the_title_is_a_heading() -> None:
    spec = make(LIST_SPEC)
    fragment = render_fragment(spec)

    assert "<!doctype" not in fragment and "<style>" not in fragment
    assert '<ul class="list">' in fragment
    assert fragment in render_page(spec)

    # A title renders a heading, and its absence renders none.
    assert '<h1 class="dashboard__title">Today</h1>' in render_page(make(LIST_SPEC))
    assert "<h1" not in render_page(make({"blocks": []}))


BOUND_SPEC = {
    "title": "Today",
    "blocks": [
        {"component": "list", "props": {"items": {"bind": "calendar.today.events"}}},
        {"component": "stat-card", "props": {"label": "Free", "value": {"bind": "disk.free"}}},
    ],
}


def test_a_local_only_value_reaches_the_page_and_was_never_in_the_spec() -> None:
    """The structural half of SPEC §6, asserted in both directions at once."""
    spec = make(BOUND_SPEC)
    values = {
        "calendar.today.events": ["therapy at 4", "dinner with Sam"],
        "disk.free": "42 GB",
    }
    before = json.dumps(spec.to_dict())
    assert "therapy" not in before, "the value was in the spec to begin with"

    page = render_page(spec, values=values)

    assert "therapy at 4" in page and "42 GB" in page
    # Rendering neither wrote a value back into the spec nor mutated it.
    assert json.dumps(spec.to_dict()) == before, "rendering wrote a value back into the spec"


def test_bindings_resolve_in_every_position_and_in_both_spellings() -> None:
    # Inside a list of items: a literal, the canonical spelling, and `{{key}}`.
    spec = make(
        {
            "blocks": [
                {"component": "list", "props": {"items": ["fixed", {"bind": "one"}, "{{two}}"]}}
            ]
        }
    )
    page = render_page(spec, values={"one": "first", "two": 2})
    assert "fixed" in page and "first" in page and "2" in page

    # Inside a row.
    spec = make(
        {
            "blocks": [
                {"component": "timeline", "props": {"entries": [[{"bind": "at"}, {"bind": "what"}]]}}
            ]
        }
    )
    page = render_page(spec, values={"at": "09:00", "what": "standup"})
    assert "09:00" in page and "standup" in page

    # The two spellings mean the same thing.
    dotted = render_page(make(BOUND_SPEC), values={"calendar.today.events": ["x"], "disk.free": "1"})
    assert "{{calendar.today.events}}" not in dotted

    # And a spec knows whether it still has any.
    assert make(BOUND_SPEC).has_bindings()
    assert not make(LIST_SPEC).has_bindings()


def test_a_malformed_or_unbound_binding_is_refused_by_name() -> None:
    # Not a dotted lower-case key, an empty key, a number, or an empty `{{ }}`.
    for bad in ({"bind": "Disk Free"}, {"bind": ""}, {"bind": 3}, "{{ }}", "{{Not.Key}}"):
        with pytest.raises(SpecError):
            make({"blocks": [{"component": "list", "props": {"items": [bad]}}]})

    # `{"bind": "x", "note": "y"}` is not a binding, and guessing would be worse.
    with pytest.raises(SpecError) as refused:
        make({"blocks": [{"component": "list", "props": {"items": [{"bind": "x", "note": "y"}]}}]})
    assert "'note'" in str(refused.value)

    # An unbound key is refused by name, and what was available is listed.
    with pytest.raises(SpecError) as refused:
        render_page(make(BOUND_SPEC), values={"disk.free": "1 GB"})
    message = str(refused.value)
    assert "'calendar.today.events'" in message
    assert "disk.free" in message, "the refusal did not list what it could have bound"

    # Called straight at a renderer: the shape a bug or a new component would take.
    with pytest.raises(SpecError) as refused:
        render(Block(component="list", props={"items": [Binding("x")]}))
    assert "never bound" in str(refused.value)


def test_every_value_that_renders_was_validated_however_it_arrived() -> None:
    spec = make({"blocks": [{"component": "list", "props": {"items": {"bind": "x"}}}]})

    # What renders is what was checked — not merely what the model proposed.
    with pytest.raises(SpecError) as refused:
        render_page(spec, values={"x": "a string, not a list"})
    assert "is a list" in str(refused.value)

    # The door is not skipped just because the value came from the owner's own store.
    with pytest.raises(SpecError):
        render_page(spec, values={"x": ["<script>alert(1)</script>"]})

    # A cross-prop rule cannot run on a key — and must not be dropped: it waits for
    # the values, then fires in the usual voice.
    spec = make(
        {"blocks": [{"component": "table", "props": {"columns": ["a"], "rows": {"bind": "rows"}}}]}
    )
    assert spec.has_bindings()
    assert "1" in render_page(spec, values={"rows": [["1"], ["2"]]})
    with pytest.raises(SpecError) as refused:
        render_page(spec, values={"rows": [["1", "2"]]})
    assert "row 1" in str(refused.value)
    assert "wide" in str(refused.value) or "1 columns" in str(refused.value)

    # A bound calendar day is held to the row shape after binding, too.
    spec = make(
        {
            "blocks": [
                {"component": "calendar", "props": {"view": "day", "days": {"bind": "calendar.days"}}}
            ]
        }
    )
    page = render_page(spec, values={"calendar.days": [["2026-10-16", "Fri", "standup"]]})
    assert "2026-10-16" in page and "standup" in page
    with pytest.raises(SpecError) as refused:
        render_page(spec, values={"calendar.days": [["2026-10-16"]]})
    assert "[date, weekday, what is on it]" in str(refused.value)


def test_the_mood_reaches_the_page_in_precedence_order() -> None:
    # An inferred mood, where the model proposed none.
    page = render_page(make(LIST_SPEC), mood="celebration")
    assert f"--d-accent: {tokens_for('celebration').accent};" in page
    assert f"--d-accent: {tokens_for('calm').accent};" not in page

    # A mood the model proposed is used when nothing else says otherwise.
    page = render_page(make({**LIST_SPEC, "mood": "midnight-focus"}))
    assert f"--d-bg: {tokens_for('midnight-focus').bg};" in page

    # An inferred mood outranks the one the model proposed.
    page = render_page(make({**LIST_SPEC, "mood": "celebration"}), mood="dense-ops")
    assert f"--d-bg: {tokens_for('dense-ops').bg};" in page

    # The owner's override outranks the inferred mood.
    page = render_page(make(LIST_SPEC), mood="dense-ops", override="low-energy")
    assert f"--d-font: {tokens_for('low-energy').font};" in page

    # An unusable mood name still renders a page.
    page = render_page(make(LIST_SPEC), mood="vibes")
    assert f"--d-bg: {tokens_for('calm').bg};" in page
