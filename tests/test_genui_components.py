"""The component vocabulary: schema, prompt and renderer agree, and nothing renders raw.

The first kind of test checks the components behave: a table pads a short row, an
empty list says so instead of rendering an empty `<ul>`, the calendar marks today.

The second kind **bypasses the door**. `spec.py` refuses markup before any renderer
sees it, so a test that only went through `Spec.parse` could never prove the
renderer escapes. These build a `Block` by hand, as a bug or a new component would,
and assert the renderer still cannot be made to emit a tag — the defence that holds
when the first one is forgotten.
"""

from __future__ import annotations

import pytest

from daedalus.genui.components import RENDERERS, VOCABULARY, render, render_blocks
from daedalus.genui.spec import Block, Component, Kind, Spec, SpecError, describe

EVIL = "<script>alert(1)</script>"


def spec_of(component: str, props: dict[str, object]) -> Spec:
    return Spec.parse({"blocks": [{"component": component, "props": props}]}, vocabulary=VOCABULARY)


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


def test_the_schema_the_prompt_and_the_renderers_all_agree() -> None:
    """The halves of a component cannot drift: one list is derived from the other."""
    assert set(VOCABULARY) == set(RENDERERS), "a component exists without its other half"

    for name, component in VOCABULARY.items():
        assert component.summary, f"{name} has no summary for the prompt"
        assert component.props, f"{name} takes no props, so it can show nothing"
        # Derived from the schema, so a component added without a renderer fails here.
        html = render_blocks(spec_of(name, minimal_props(name, component)))
        assert html.strip(), f"{name} rendered nothing"

    # The prompt writes down what the validator enforces: `?` for optional props and
    # a closed set spelled out, so a model can hit it first time.
    text = describe(VOCABULARY)
    assert "note: text?" in text, "an optional prop is not marked optional"
    assert "view: text[day|week|month]" in text, "a closed set is not written down for the model"
    assert "value: text" in text


def test_the_door_refuses_a_prop_that_is_missing_or_unknown_and_names_it() -> None:
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

    for name, component in VOCABULARY.items():
        props = minimal_props(name, component)
        props["styling"] = "color:red"
        with pytest.raises(SpecError) as refused:
            spec_of(name, props)
        assert "'styling'" in str(refused.value)


EVIL_PROPS = [
    ("stat-card", {"label": EVIL, "value": EVIL, "note": EVIL}),
    ("list", {"items": [EVIL], "empty": EVIL}),
    ("timeline", {"entries": [[EVIL, EVIL]], "empty": EVIL}),
    ("table", {"columns": [EVIL], "rows": [[EVIL]]}),
    ("prose", {"text": EVIL, "heading": EVIL}),
    ("calendar", {"view": "day", "days": [["2026-10-16", "Fri", EVIL]], "today": "2026-10-16"}),
]


def test_no_component_can_be_made_to_emit_a_tag_even_with_the_door_bypassed() -> None:
    """The renderer's own escape, proved by walking past the validator."""
    for component, props in EVIL_PROPS:
        html = render(Block(component=component, props=props))

        assert "<script" not in html, f"{component} rendered a tag"
        assert EVIL not in html
        assert "&lt;script" in html, f"{component} dropped the value instead of escaping it"

    # `escape` quotes by default — an unquoted escape is how a quote becomes an attribute.
    html = render(Block(component="prose", props={"text": '" onmouseover="steal()'}))
    assert 'onmouseover="steal()"' not in html
    assert "&quot;" in html


def test_the_door_refuses_markup_before_any_renderer_sees_it() -> None:
    for component, props in EVIL_PROPS:
        with pytest.raises(SpecError):
            spec_of(component, props)


def test_a_component_renders_its_value_omits_absent_optionals_and_keeps_order() -> None:
    # stat-card: the value and label are stated, and a note it was not given is omitted.
    html = render_blocks(spec_of("stat-card", {"label": "Free", "value": "42 GB"}))
    assert "42 GB" in html and "Free" in html
    assert "stat-card__note" not in html

    # A number renders as a number, not as Python's `None`.
    html = render(Block(component="stat-card", props={"label": "Events", "value": 12}))
    assert "12" in html and "None" not in html

    # list: the lines in order, one <li> each.
    html = render_blocks(spec_of("list", {"items": ["standup", "review"]}))
    assert html.index("standup") < html.index("review")
    assert html.count("<li") == 2

    # timeline: entry order kept, and a one-cell entry invents no second cell.
    html = render_blocks(spec_of("timeline", {"entries": [["09:00", "standup"], ["14:00", "review"]]}))
    assert html.index("09:00") < html.index("14:00")
    assert "timeline__when\">09:00" in html
    html = render_blocks(spec_of("timeline", {"entries": [["09:00"]]}))
    assert "09:00" in html
    assert "None" not in html

    # prose: the heading is optional.
    assert "<h2" not in render_blocks(spec_of("prose", {"text": "All clear."}))
    assert "<h2" in render_blocks(spec_of("prose", {"text": "All clear.", "heading": "Today"}))

    # table: a short row is padded to the headings, and a wide row is refused rather
    # than silently dropping the owner's value.
    html = render_blocks(
        spec_of("table", {"columns": ["a", "b", "c"], "rows": [["1"], ["1", "2", "3"]]})
    )
    assert html.count('<th scope="col">') == 3
    assert html.count("<td>") == 6, "a short row was not padded to the headings"
    with pytest.raises(SpecError) as refused:
        spec_of("table", {"columns": ["a"], "rows": [["1", "2"]]})
    assert "row 1" in str(refused.value)
    assert "1 columns" in str(refused.value)


def test_a_component_with_nothing_to_show_says_so_instead_of_an_empty_element() -> None:
    html = render_blocks(spec_of("list", {"items": [], "empty": "No tasks."}))
    assert "No tasks." in html
    assert "<ul" not in html

    html = render_blocks(spec_of("timeline", {"entries": [], "empty": "Nothing on."}))
    assert "Nothing on." in html
    assert "<ol" not in html


def test_the_calendar_marks_today_and_holds_its_views_and_day_shape() -> None:
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

    # Every view the closed set names is accepted.
    for view in ("day", "week", "month"):
        html = render_blocks(spec_of("calendar", {"view": view, "days": [["2026-10-16", "Fri", ""]]}))
        assert f"calendar--{view}" in html

    # A view it does not have is refused, and the three it does are named.
    with pytest.raises(SpecError) as refused:
        spec_of("calendar", {"view": "year", "days": [["2026-10-16", "Fri", ""]]})
    message = str(refused.value)
    assert "day | week | month" in message
    assert "'year'" in message

    # A day is [date, weekday, what is on it] — three cells, every time.
    with pytest.raises(SpecError) as refused:
        spec_of("calendar", {"view": "day", "days": [["2026-10-16"]]})
    assert "day 1" in str(refused.value)
    assert "[date, weekday, what is on it]" in str(refused.value)
