"""The renderer: one self-contained page, and values that never left the laptop.

The test that matters most here is the pair in
`test_a_local_only_value_reaches_the_page_and_was_never_in_the_spec`. SPEC §6
says a `local_only` fact never enters a remote-bound prompt, and the honest way to
keep that promise is for the fact to be absent from the spec *entirely* — the
model composes `{"bind": "calendar.today.events"}` and a local lookup fills it in.
So the test asserts both halves at once: the page contains the value, and the
spec the model held does not — before *and* after rendering.

The rest is the containment promise: no scripts, no external anything, and a
Content-Security-Policy that makes the browser enforce it rather than asking it
to.
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


# -- one self-contained document ---------------------------------------------


def test_a_rendered_page_is_a_whole_document() -> None:
    page = render_page(make(LIST_SPEC))

    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")
    assert '<html lang="en">' in page
    assert page.count("<style>") == 1
    assert "<title>Today</title>" in page


def test_the_page_fetches_nothing() -> None:
    """Self-contained is the promise; nothing may reach out for anything."""
    page = render_page(make(LIST_SPEC))

    for forbidden in ("<script", "<link", "@import", "url(", "http://", "https://", "src="):
        assert forbidden not in page, f"the page contains {forbidden!r}"


def test_the_policy_makes_the_browser_enforce_it_too() -> None:
    """A test that asserts a page is safe is still a test; the browser is the guarantee."""
    page = render_page(make(LIST_SPEC))

    assert 'http-equiv="Content-Security-Policy"' in page
    # Attribute-escaped, which the parser decodes before the policy is read —
    # `'none'` arrives as `&#x27;none&#x27;` and means the same thing.
    policy = unescape(page)
    assert "default-src 'none'" in policy
    assert "script-src" not in policy, "a script source was allowed to something"
    assert "form-action 'none'" in policy
    assert CSP in policy


def test_the_page_is_built_for_a_phone_as_well_as_a_laptop() -> None:
    page = render_page(make(LIST_SPEC))

    assert 'name="viewport"' in page
    assert "width=device-width" in page


def test_an_empty_spec_still_makes_a_valid_page() -> None:
    page = render_page(make({"blocks": []}))

    assert page.startswith("<!doctype html>")
    assert "<main class=\"dashboard\">" in page
    assert "Daedalus" in page, "an untitled page should still have a title"


def test_the_title_renders_a_heading_and_its_absence_renders_none() -> None:
    assert '<h1 class="dashboard__title">Today</h1>' in render_page(make(LIST_SPEC))
    assert "<h1" not in render_page(make({"blocks": []}))


def test_the_fragment_is_the_page_without_the_document() -> None:
    spec = make(LIST_SPEC)
    fragment = render_fragment(spec)

    assert "<!doctype" not in fragment and "<style>" not in fragment
    assert "<ul class=\"list\">" in fragment
    assert fragment in render_page(spec)


# -- values stay local -------------------------------------------------------


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

    assert "therapy" not in json.dumps(spec.to_dict()), "the value was in the spec to begin with"

    page = render_page(spec, values=values)

    assert "therapy at 4" in page and "42 GB" in page
    assert "therapy" not in json.dumps(spec.to_dict()), "rendering wrote a value back into the spec"


def test_rendering_does_not_mutate_the_spec_it_was_given() -> None:
    spec = make(BOUND_SPEC)
    before = json.dumps(spec.to_dict())

    render_page(spec, values={"calendar.today.events": ["x"], "disk.free": "1 GB"})

    assert json.dumps(spec.to_dict()) == before


def test_bindings_resolve_inside_a_list_of_items() -> None:
    spec = make(
        {
            "blocks": [
                {
                    "component": "list",
                    "props": {"items": ["fixed", {"bind": "one"}, "{{two}}"]},
                }
            ]
        }
    )

    page = render_page(spec, values={"one": "first", "two": 2})

    assert "fixed" in page and "first" in page and "2" in page


def test_bindings_resolve_inside_a_row() -> None:
    spec = make(
        {
            "blocks": [
                {
                    "component": "timeline",
                    "props": {"entries": [[{"bind": "at"}, {"bind": "what"}]]},
                }
            ]
        }
    )

    page = render_page(spec, values={"at": "09:00", "what": "standup"})

    assert "09:00" in page and "standup" in page


def test_the_two_binding_spellings_mean_the_same_thing() -> None:
    dotted = render_page(make(BOUND_SPEC), values={"calendar.today.events": ["x"], "disk.free": "1"})

    assert "{{calendar.today.events}}" not in dotted


def test_an_unbound_key_is_refused_by_name_and_what_is_available_is_listed() -> None:
    with pytest.raises(SpecError) as refused:
        render_page(make(BOUND_SPEC), values={"disk.free": "1 GB"})

    message = str(refused.value)
    assert "'calendar.today.events'" in message
    assert "disk.free" in message, "the refusal did not list what it could have bound"


@pytest.mark.parametrize("bad", [{"bind": "Disk Free"}, {"bind": ""}, {"bind": 3}, "{{ }}", "{{Not.Key}}"])
def test_a_malformed_binding_is_refused(bad: object) -> None:
    with pytest.raises(SpecError):
        make({"blocks": [{"component": "list", "props": {"items": [bad]}}]})


def test_a_binding_that_carries_extra_keys_is_refused() -> None:
    """`{"bind": "x", "note": "y"}` is not a binding, and guessing would be worse."""
    with pytest.raises(SpecError) as refused:
        make({"blocks": [{"component": "list", "props": {"items": [{"bind": "x", "note": "y"}]}}]})

    assert "'note'" in str(refused.value)


def test_a_bound_value_is_still_validated_by_the_same_door() -> None:
    """What renders is what was checked — not merely what the model proposed."""
    spec = make({"blocks": [{"component": "list", "props": {"items": {"bind": "x"}}}]})

    with pytest.raises(SpecError) as refused:
        render_page(spec, values={"x": "a string, not a list"})

    assert "is a list" in str(refused.value)


def test_a_bound_value_carrying_markup_is_refused_too() -> None:
    """The door is not skipped just because the value came from the owner's own store."""
    spec = make({"blocks": [{"component": "list", "props": {"items": {"bind": "x"}}}]})

    with pytest.raises(SpecError):
        render_page(spec, values={"x": ["<script>alert(1)</script>"]})


def test_a_binding_that_never_got_bound_cannot_reach_the_page() -> None:
    """Called straight at a renderer — the shape a bug or a new component would take."""
    with pytest.raises(SpecError) as refused:
        render(Block(component="list", props={"items": [Binding("x")]}))

    assert "never bound" in str(refused.value)


def test_a_spec_knows_whether_it_still_has_bindings() -> None:
    assert make(BOUND_SPEC).has_bindings()
    assert not make(LIST_SPEC).has_bindings()


def test_a_cross_prop_rule_waits_for_the_values_and_then_still_fires() -> None:
    """A rule about a *shape* cannot run on a key — and must not be dropped."""
    spec = make(
        {
            "blocks": [
                {"component": "table", "props": {"columns": ["a"], "rows": {"bind": "rows"}}}
            ]
        }
    )

    # Parsing succeeds: the prop is a key, so there is nothing to check yet, and
    # running the rule now would be a TypeError on a Binding, not a refusal.
    assert spec.has_bindings()
    assert "1" in render_page(spec, values={"rows": [["1"], ["2"]]})

    # But the rule is not skipped, only deferred: binding re-parses with values,
    # and a row wider than the headings is refused there in the usual voice.
    with pytest.raises(SpecError) as refused:
        render_page(spec, values={"rows": [["1", "2"]]})

    assert "row 1" in str(refused.value)
    assert "wide" in str(refused.value) or "1 columns" in str(refused.value)


def test_a_bound_calendar_day_is_held_to_the_row_shape_after_binding() -> None:
    spec = make(
        {
            "blocks": [
                {
                    "component": "calendar",
                    "props": {"view": "day", "days": {"bind": "calendar.days"}},
                }
            ]
        }
    )

    page = render_page(spec, values={"calendar.days": [["2026-10-16", "Fri", "standup"]]})
    assert "2026-10-16" in page and "standup" in page

    with pytest.raises(SpecError) as refused:
        render_page(spec, values={"calendar.days": [["2026-10-16"]]})

    assert "[date, weekday, what is on it]" in str(refused.value)


# -- the mood reaches the page -----------------------------------------------


def test_the_mood_reaches_the_page() -> None:
    page = render_page(make(LIST_SPEC), mood="celebration")

    assert f"--d-accent: {tokens_for('celebration').accent};" in page
    assert "--d-accent: #2f5fd0;" not in page


def test_a_mood_the_model_proposed_is_used_when_nothing_else_says_otherwise() -> None:
    page = render_page(make({**LIST_SPEC, "mood": "midnight-focus"}))

    assert f"--d-bg: {tokens_for('midnight-focus').bg};" in page


def test_an_inferred_mood_outranks_the_one_the_model_proposed() -> None:
    page = render_page(make({**LIST_SPEC, "mood": "celebration"}), mood="dense-ops")

    assert f"--d-bg: {tokens_for('dense-ops').bg};" in page


def test_the_owner_override_outranks_the_inferred_mood() -> None:
    page = render_page(make(LIST_SPEC), mood="dense-ops", override="low-energy")

    assert f"--d-font: {tokens_for('low-energy').font};" in page


def test_an_unusable_mood_name_still_renders_a_page() -> None:
    page = render_page(make(LIST_SPEC), mood="vibes")

    assert f"--d-bg: {tokens_for('calm').bg};" in page


def test_the_page_carries_no_comment_that_leaks_its_own_composition() -> None:
    """A rendered page is the owner's; it should not narrate how it was assembled."""
    page = render_page(make(LIST_SPEC), values={})

    assert "<!--" not in page
