"""The component vocabulary: schema and renderer agree, and nothing renders raw.

Two kinds of test here, and the second kind matters more.

The first kind checks the components behave: a table pads a short row, a calendar
refuses a day that is not three cells, an empty list says so instead of rendering
an empty `<ul>`.

The second kind **bypasses the door**. Every value in a spec is refused for
markup by `spec.py` before it ever reaches a renderer — so a test that only goes
through `Spec.parse` can never prove the renderer escapes anything. These tests
build a `Block` by hand, as a bug or a new component would, and assert the
renderer still cannot be made to emit a tag. That is the defence that holds when
the first one is forgotten.
"""

from __future__ import annotations

import pytest

from daedalus.genui.components import RENDERERS, VOCABULARY, render, render_blocks
from daedalus.genui.spec import Block, Component, Kind, Spec, SpecError, describe

EVIL = "<script>alert(1)</script>"


def spec_of(component: str, props: dict[str, object]) -> Spec:
    return Spec.parse({"blocks": [{"component": component, "props": props}]}, vocabulary=VOCABULARY)


# -- the vocabulary is coherent ---------------------------------------------


def test_every_component_has_a_renderer_and_every_renderer_has_a_schema() -> None:
    """The two halves of a component cannot drift: one list is derived from the other."""
    assert set(VOCABULARY) == set(RENDERERS), "a component exists without its other half"


def test_every_component_declares_a_summary_and_at_least_one_prop() -> None:
    for name, component in VOCABULARY.items():
        assert component.summary, f"{name} has no summary for the prompt"
        assert component.props, f"{name} takes no props, so it can show nothing"


SAMPLES = {
    Kind.TEXT: "text",
    Kind.LINK: "/day",
    Kind.NUMBER: 1,
    Kind.BOOL: True,
    Kind.LIST: ["one"],
    Kind.ROWS: [["one"]],
}

#: Where a component's own cross-prop rule makes the generic sample invalid.
OVERRIDES: dict[str, dict[str, object]] = {
    "calendar": {"view": "day", "days": [["2026-10-16", "Fri", "standup"]]},
}


def minimal_props(name: str, component: Component) -> dict[str, object]:
    """The fewest props a component can be given, derived from its own schema."""
    if name in OVERRIDES:
        return dict(OVERRIDES[name])
    return {key: SAMPLES[component.props[key].kind] for key in component.required()}


def test_every_component_renders_from_a_minimal_valid_spec() -> None:
    """Derived from the schema, so a component added without a renderer fails here."""
    for name, component in VOCABULARY.items():
        html = render_blocks(spec_of(name, minimal_props(name, component)))
        assert html.strip(), f"{name} rendered nothing"


def test_omitting_any_required_prop_is_refused_naming_it() -> None:
    for name, component in VOCABULARY.items():
        for missing in component.required():
            props = {
                key: value
                for key, value in minimal_props(name, component).items()
                if key != missing
            }
            with pytest.raises(SpecError) as refused:
                spec_of(name, props)
            assert repr(missing) in str(refused.value), f"{name} did not name {missing}"


def test_an_unknown_prop_is_refused_for_every_component() -> None:
    for name, component in VOCABULARY.items():
        props = minimal_props(name, component)
        props["styling"] = "color:red"
        with pytest.raises(SpecError) as refused:
            spec_of(name, props)
        assert "'styling'" in str(refused.value)


# -- nothing can be made to emit a tag --------------------------------------

EVIL_PROPS = [
    ("stat-card", {"label": EVIL, "value": EVIL, "note": EVIL}),
    ("list", {"items": [EVIL], "empty": EVIL}),
    ("timeline", {"entries": [[EVIL, EVIL]], "empty": EVIL}),
    ("table", {"columns": [EVIL], "rows": [[EVIL]]}),
    ("prose", {"text": EVIL, "heading": EVIL}),
    ("calendar", {"view": "day", "days": [["2026-10-16", "Fri", EVIL]], "today": "2026-10-16"}),
]


@pytest.mark.parametrize("component,props", EVIL_PROPS)
def test_no_component_can_be_made_to_emit_a_tag_even_with_the_door_bypassed(
    component: str, props: dict[str, object]
) -> None:
    """The renderer's own escape, proved by walking past the validator."""
    html = render(Block(component=component, props=props))

    assert "<script" not in html, f"{component} rendered a tag"
    assert EVIL not in html
    assert "&lt;script" in html, f"{component} dropped the value instead of escaping it"


def test_an_attribute_value_cannot_break_out_of_its_quotes() -> None:
    """`escape` quotes by default — an unquoted escape is how a class becomes an attribute."""
    html = render(Block(component="prose", props={"text": '\" onmouseover="steal()'}))

    assert "onmouseover=\"steal()\"" not in html
    assert "&quot;" in html


# -- the door still holds these ---------------------------------------------


@pytest.mark.parametrize("component,props", EVIL_PROPS)
def test_the_door_refuses_the_same_values_before_a_renderer_sees_them(
    component: str, props: dict[str, object]
) -> None:
    with pytest.raises(SpecError):
        spec_of(component, props)


# -- individual components --------------------------------------------------


def test_a_stat_card_states_a_value_and_omits_a_note_it_was_not_given() -> None:
    html = render_blocks(spec_of("stat-card", {"label": "Free", "value": "42 GB"}))

    assert "42 GB" in html and "Free" in html
    assert "stat-card__note" not in html


def test_a_list_says_nothing_to_show_rather_than_drawing_an_empty_list() -> None:
    html = render_blocks(spec_of("list", {"items": [], "empty": "No tasks."}))

    assert "No tasks." in html
    assert "<ul" not in html


def test_a_list_renders_its_lines_in_order() -> None:
    html = render_blocks(spec_of("list", {"items": ["standup", "review"]}))

    assert html.index("standup") < html.index("review")
    assert html.count("<li") == 2


def test_a_stat_card_renders_a_number_as_a_number() -> None:
    html = render(Block(component="stat-card", props={"label": "Events", "value": 12}))

    assert "12" in html and "None" not in html


def test_a_table_shows_headings_and_pads_a_short_row() -> None:
    html = render_blocks(
        spec_of("table", {"columns": ["a", "b", "c"], "rows": [["1"], ["1", "2", "3"]]})
    )

    assert html.count('<th scope="col">') == 3
    assert html.count("<td>") == 6, "a short row was not padded to the headings"
    assert "</td>" in html


def test_a_table_refuses_a_row_wider_than_its_headings_rather_than_dropping_a_value() -> None:
    with pytest.raises(SpecError) as refused:
        spec_of("table", {"columns": ["a"], "rows": [["1", "2"]]})

    assert "row 1" in str(refused.value)
    assert "1 columns" in str(refused.value)


def test_a_timeline_states_an_empty_day() -> None:
    html = render_blocks(spec_of("timeline", {"entries": [], "empty": "Nothing on."}))

    assert "Nothing on." in html
    assert "<ol" not in html


def test_a_timeline_keeps_the_entry_order_it_was_given() -> None:
    html = render_blocks(spec_of("timeline", {"entries": [["09:00", "standup"], ["14:00", "review"]]}))

    assert html.index("09:00") < html.index("14:00")
    assert "timeline__when\">09:00" in html


def test_a_timeline_entry_of_only_a_time_does_not_invent_a_second_cell() -> None:
    html = render_blocks(spec_of("timeline", {"entries": [["09:00"]]}))

    assert "09:00" in html
    assert "None" not in html


def test_prose_renders_an_optional_heading() -> None:
    without = render_blocks(spec_of("prose", {"text": "All clear."}))
    with_heading = render_blocks(spec_of("prose", {"text": "All clear.", "heading": "Today"}))

    assert "<h2" not in without
    assert "<h2" in with_heading


def test_the_calendar_marks_today_and_says_how_much_time_it_shows() -> None:
    html = render_blocks(
        spec_of(
            "calendar",
            {
                "view": "week",
                "days": [["2026-10-16", "Fri", "standup"], ["2026-10-17", "Sat", ""]],
                "today": "2026-10-16",
            },
        )
    )

    assert "calendar--week" in html
    assert html.count("calendar__day--today") == 1
    assert 'calendar__day--today"><span class="calendar__date">2026-10-16' in html


def test_the_calendar_refuses_a_view_it_does_not_have_and_names_the_three_it_does() -> None:
    with pytest.raises(SpecError) as refused:
        spec_of("calendar", {"view": "year", "days": [["2026-10-16", "Fri", ""]]})

    message = str(refused.value)
    assert "day | week | month" in message
    assert "'year'" in message


@pytest.mark.parametrize("view", ["day", "week", "month"])
def test_every_calendar_view_is_accepted(view: str) -> None:
    html = render_blocks(spec_of("calendar", {"view": view, "days": [["2026-10-16", "Fri", ""]]}))
    assert f"calendar--{view}" in html


def test_the_calendar_refuses_a_day_that_is_not_the_declared_shape() -> None:
    with pytest.raises(SpecError) as refused:
        spec_of("calendar", {"view": "day", "days": [["2026-10-16"]]})

    assert "day 1" in str(refused.value)
    assert "[date, weekday, what is on it]" in str(refused.value)


# -- the prompt and the validator agree -------------------------------------


def test_describe_marks_optional_props_and_names_a_closed_set_of_choices() -> None:
    text = describe(VOCABULARY)

    assert "note: text?" in text, "an optional prop is not marked optional"
    assert "view: text[day|week|month]" in text, "a closed set is not written down for the model"
    assert "value: text" in text
