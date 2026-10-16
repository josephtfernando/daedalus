"""The spec boundary: the model composes structure, and that is all it composes.

These tests exist because the rule in SPEC §3.6 — *the model proposes a UI spec,
a deterministic renderer disposes* — is only true if something refuses a proposal
that steps outside the vocabulary. So the tests are mostly about **refusals**:
the unknown component, the unknown prop, the value that is secretly a tag, the
link that is secretly a scheme. A refusal that names the way out is the point; a
refusal that says only "invalid" is how a model ends up retrying the same call.
"""

from __future__ import annotations

import pytest

from daedalus.genui.spec import (
    MAX_BLOCKS,
    MAX_ITEMS,
    Component,
    Kind,
    Prop,
    Spec,
    SpecError,
    describe,
)

VOCABULARY: dict[str, Component] = {
    "stat-card": Component(
        name="stat-card",
        summary="one value, labelled",
        props={
            "label": Prop(Kind.TEXT, required=True),
            "value": Prop(Kind.TEXT, required=True),
            "note": Prop(Kind.TEXT),
        },
    ),
    "list": Component(
        name="list",
        summary="a list of lines",
        props={"items": Prop(Kind.LIST, required=True)},
    ),
    "table": Component(
        name="table",
        summary="rows under headings",
        props={
            "columns": Prop(Kind.LIST, required=True),
            "rows": Prop(Kind.ROWS, required=True),
        },
    ),
    "day-link": Component(
        name="day-link",
        summary="a link to a day",
        props={"href": Prop(Kind.LINK, required=True), "count": Prop(Kind.NUMBER)},
    ),
}


def parse(raw: object) -> Spec:
    return Spec.parse(raw, vocabulary=VOCABULARY)


# -- the happy path ---------------------------------------------------------


def test_a_simple_spec_parses_and_round_trips_through_json() -> None:
    spec = parse(
        {
            "title": "Today",
            "mood": "calm",
            "blocks": [
                {"component": "stat-card", "props": {"label": "Free", "value": "42 GB"}},
                {"component": "list", "props": {"items": ["standup", "review"]}},
            ],
        }
    )

    assert [block.component for block in spec.blocks] == ["stat-card", "list"]
    assert spec.title == "Today"
    again = parse(spec.to_dict())
    assert again == spec


def test_a_prop_that_is_not_required_may_be_left_out() -> None:
    spec = parse({"blocks": [{"component": "stat-card", "props": {"label": "a", "value": "b"}}]})
    assert spec.blocks[0].props == {"label": "a", "value": "b"}


def test_a_lone_block_is_accepted_as_a_one_block_page() -> None:
    """A model that composes one thing should not have to wrap it in a list."""
    spec = parse({"blocks": {"component": "list", "props": {"items": ["a"]}}})
    assert len(spec.blocks) == 1


def test_the_two_spellings_a_model_reaches_for_are_both_accepted() -> None:
    """`type` for `component`, `components` for `blocks` — a synonym is not a sin.

    Learning the name is free; refusing the synonym costs a step and teaches
    nothing, and a step lost to vocabulary is a step the owner paid for.
    """
    spec = parse({"components": [{"type": "list", "props": {"items": ["a"]}}]})
    assert spec.blocks[0].component == "list"


def test_values_are_trimmed_and_empty_defaults_stay_empty() -> None:
    spec = parse({"blocks": [{"component": "list", "props": {"items": ["  a  "]}}]})
    assert spec.blocks[0].props["items"] == ["a"]
    assert parse({"blocks": []}).title == ""
    assert parse({"blocks": []}).mood == ""


def test_a_props_key_of_none_is_read_as_no_props_rather_than_a_type_error() -> None:
    """`props: null` is a model meaning "nothing extra", not a malformed block."""
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": None}]})

    assert "missing" in str(refused.value)
    assert "is an object" not in str(refused.value)


# -- refusals name the way out ----------------------------------------------


def test_an_unknown_component_is_refused_by_name_and_the_vocabulary_is_offered() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "chart", "props": {}}]})

    message = str(refused.value)
    assert "'chart'" in message
    assert "stat-card" in message and "list" in message


def test_an_unknown_prop_is_refused_and_the_real_props_are_listed() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": [], "colour": "red"}}]})

    message = str(refused.value)
    assert "'colour'" in message
    assert "It takes: items" in message


def test_a_missing_required_prop_is_refused_and_the_real_props_are_listed() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "stat-card", "props": {"label": "Free"}}]})

    assert "'value'" in str(refused.value)


def test_a_refusal_says_what_it_received_when_the_component_name_is_simply_absent() -> None:
    """The lesson from the calendar: name the keys that arrived, not just the gap."""
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"title": "Today", "props": {}}]})

    message = str(refused.value)
    assert "'title'" in message, "the message did not say what it actually received"
    assert "vocabulary" in message


def test_an_unknown_top_level_key_is_refused_rather_than_ignored() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [], "styles": "body{color:red}"})

    assert "'styles'" in str(refused.value)


def test_a_spec_that_is_not_an_object_is_refused_with_its_type() -> None:
    with pytest.raises(SpecError):
        parse(["list"])
    with pytest.raises(SpecError) as refused:
        parse("a dashboard please")
    assert "text" in str(refused.value)


def test_a_block_that_is_not_an_object_is_refused_with_its_position() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": ["list"]})

    assert "block 1" in str(refused.value)


# -- the model never emits markup -------------------------------------------


def test_a_value_carrying_a_script_tag_is_refused_at_the_door() -> None:
    """Refused here *and* escaped at render. Two defences, and this is the first."""
    with pytest.raises(SpecError) as refused:
        parse(
            {
                "blocks": [
                    {"component": "list", "props": {"items": ["<script>fetch('x')</script>"]}}
                ]
            }
        )

    assert "markup" in str(refused.value)


@pytest.mark.parametrize(
    "value",
    ["<b>bold</b>", "< img src=x onerror=y>", "<!doctype html>", "</div>"],
)
def test_markup_in_a_value_is_refused_however_it_is_spelled(value: str) -> None:
    with pytest.raises(SpecError):
        parse({"blocks": [{"component": "list", "props": {"items": [value]}}]})


@pytest.mark.parametrize("value", ["dinner <-> party", "12 < 15", "a > b", "5 <= x"])
def test_a_less_than_sign_that_is_not_a_tag_is_still_a_value(value: str) -> None:
    """The guard is for tags. Refusing arithmetic would make the vocabulary useless."""
    spec = parse({"blocks": [{"component": "list", "props": {"items": [value]}}]})
    assert spec.blocks[0].props["items"] == [value]


@pytest.mark.parametrize("value", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x"])
def test_a_value_carrying_a_scheme_is_refused(value: str) -> None:
    with pytest.raises(SpecError):
        parse({"blocks": [{"component": "list", "props": {"items": [value]}}]})


def test_a_link_must_be_a_path_on_this_server() -> None:
    assert parse({"blocks": [{"component": "day-link", "props": {"href": "/day/2026-10-16"}}]})

    for bad in ("https://example.com/x", "//example.com/x", "day/16", "javascript:alert(1)"):
        with pytest.raises(SpecError):
            parse({"blocks": [{"component": "day-link", "props": {"href": bad}}]})


# -- types ------------------------------------------------------------------


def test_true_is_not_a_number_even_though_python_says_it_is() -> None:
    """`isinstance(True, int)` is True in Python, so this is a real trap, not a style point."""
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "day-link", "props": {"href": "/d", "count": True}}]})

    assert "number" in str(refused.value)
    assert parse({"blocks": [{"component": "day-link", "props": {"href": "/d", "count": 3}}]})


def test_text_is_not_a_number() -> None:
    with pytest.raises(SpecError):
        parse({"blocks": [{"component": "day-link", "props": {"href": "/d", "count": "3"}}]})


def test_a_list_prop_must_be_a_list() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": "standup"}}]})

    assert "is a list" in str(refused.value)


def test_a_cell_cannot_be_a_nested_structure() -> None:
    """A cell is a value. Nested structure is how a spec becomes a document language."""
    with pytest.raises(SpecError) as refused:
        parse(
            {
                "blocks": [
                    {
                        "component": "table",
                        "props": {"columns": ["a"], "rows": [[{"deep": "thing"}]]},
                    }
                ]
            }
        )

    assert "nested" in str(refused.value)


def test_a_row_that_is_not_a_row_is_refused_with_its_position() -> None:
    with pytest.raises(SpecError) as refused:
        parse(
            {
                "blocks": [
                    {"component": "table", "props": {"columns": ["a"], "rows": ["not a row"]}}
                ]
            }
        )

    assert "rows[0]" in str(refused.value)


def test_booleans_and_numbers_survive_as_cells() -> None:
    spec = parse(
        {
            "blocks": [
                {"component": "table", "props": {"columns": ["a", "b"], "rows": [[True, 3]]}}
            ]
        }
    )
    assert spec.blocks[0].props["rows"] == [[True, 3]]


# -- the page is bounded ----------------------------------------------------


def test_too_many_blocks_is_refused_and_the_limit_is_named() -> None:
    block = {"component": "list", "props": {"items": ["a"]}}
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [block] * (MAX_BLOCKS + 1)})

    assert str(MAX_BLOCKS) in str(refused.value)


def test_a_list_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": ["a"] * (MAX_ITEMS + 1)}}]})

    assert str(MAX_ITEMS) in str(refused.value)


def test_a_value_longer_than_the_limit_is_refused_rather_than_truncated() -> None:
    with pytest.raises(SpecError):
        parse({"blocks": [{"component": "list", "props": {"items": ["x" * 5000]}}]})


# -- the vocabulary is teachable -------------------------------------------


def test_describe_names_every_component_and_marks_which_props_may_be_omitted() -> None:
    text = describe(VOCABULARY)

    for name in VOCABULARY:
        assert name in text
    assert "value: text" in text
    assert "note: text?" in text, "an optional prop is not marked as optional"
    assert "never markup" in text


def test_describe_is_stable_so_the_prompt_does_not_churn() -> None:
    assert describe(VOCABULARY) == describe(VOCABULARY)
    assert describe(VOCABULARY).index("- day-link") < describe(VOCABULARY).index("- list")


def test_describe_says_how_a_block_is_written() -> None:
    text = describe(VOCABULARY)
    assert '"component"' in text
    assert '"props"' in text
