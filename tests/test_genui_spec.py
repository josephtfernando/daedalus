"""The spec boundary: the model composes structure, and that is all it composes.

SPEC §3.6 says the model proposes a UI spec and a deterministic renderer disposes,
which is only true if something refuses a proposal outside the vocabulary. So these
are mostly about **refusals**: the unknown component, the unknown prop, the value
that is secretly a tag, the link that is secretly a scheme — and a refusal names the
way out, because one that says only "invalid" is how a model retries the same call.
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


def test_a_valid_spec_parses_and_round_trips_and_meets_a_model_halfway() -> None:
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
    assert parse(spec.to_dict()) == spec

    # A prop that is not required may be left out.
    optional = parse({"blocks": [{"component": "stat-card", "props": {"label": "a", "value": "b"}}]})
    assert optional.blocks[0].props == {"label": "a", "value": "b"}

    # A lone block is a one-block page; a model should not have to wrap it in a list.
    assert len(parse({"blocks": {"component": "list", "props": {"items": ["a"]}}}).blocks) == 1

    # The two spellings a model reaches for: `type` for `component`, `components`
    # for `blocks` — a synonym is not a sin.
    synonym = parse({"components": [{"type": "list", "props": {"items": ["a"]}}]})
    assert synonym.blocks[0].component == "list"

    # Values are trimmed, and an empty page keeps empty defaults.
    trimmed = parse({"blocks": [{"component": "list", "props": {"items": ["  a  "]}}]})
    assert trimmed.blocks[0].props["items"] == ["a"]
    assert parse({"blocks": []}).title == ""
    assert parse({"blocks": []}).mood == ""


def test_a_refusal_names_the_vocabulary_and_what_it_actually_received() -> None:
    # The unknown component, with the real names offered.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "chart", "props": {}}]})
    message = str(refused.value)
    assert "'chart'" in message
    assert "stat-card" in message and "list" in message

    # The unknown prop, with the real props listed.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": [], "colour": "red"}}]})
    message = str(refused.value)
    assert "'colour'" in message
    assert "It takes: items" in message

    # The missing required prop, named.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "stat-card", "props": {"label": "Free"}}]})
    assert "'value'" in str(refused.value)

    # The component name simply absent: name the keys that arrived, not just the gap.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"title": "Today", "props": {}}]})
    message = str(refused.value)
    assert "'title'" in message, "the message did not say what it actually received"
    assert "vocabulary" in message


def test_a_spec_of_the_wrong_shape_is_refused_with_its_type_or_its_position() -> None:
    # `props: null` is a model meaning "nothing extra": it is read as no props, so
    # the refusal is about the missing required prop, not about a type error.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": None}]})
    assert "missing" in str(refused.value)
    assert "is an object" not in str(refused.value)

    # An unknown top-level key is refused rather than ignored.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [], "styles": "body{color:red}"})
    assert "'styles'" in str(refused.value)

    # A spec that is not an object is refused with its type.
    with pytest.raises(SpecError):
        parse(["list"])
    with pytest.raises(SpecError) as refused:
        parse("a dashboard please")
    assert "text" in str(refused.value)

    # A block that is not an object is refused with its position.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": ["list"]})
    assert "block 1" in str(refused.value)


def test_a_value_is_text_and_never_carries_markup_or_a_scheme() -> None:
    # Refused here *and* escaped at render. Two defences, and this is the first.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": ["<script>fetch('x')</script>"]}}]})
    assert "markup" in str(refused.value)

    # Markup is refused however it is spelled.
    for value in ("<b>bold</b>", "< img src=x onerror=y>", "<!doctype html>", "</div>"):
        with pytest.raises(SpecError):
            parse({"blocks": [{"component": "list", "props": {"items": [value]}}]})

    # A URI scheme in a value is refused, whatever its casing.
    for value in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x"):
        with pytest.raises(SpecError):
            parse({"blocks": [{"component": "list", "props": {"items": [value]}}]})

    # The guard is for tags; refusing arithmetic would make the vocabulary useless.
    for value in ("dinner <-> party", "12 < 15", "a > b", "5 <= x"):
        spec = parse({"blocks": [{"component": "list", "props": {"items": [value]}}]})
        assert spec.blocks[0].props["items"] == [value]


def test_a_link_is_a_path_on_this_server_and_nothing_else() -> None:
    assert parse({"blocks": [{"component": "day-link", "props": {"href": "/day/2026-10-16"}}]})

    for bad in ("https://example.com/x", "//example.com/x", "day/16", "javascript:alert(1)"):
        with pytest.raises(SpecError):
            parse({"blocks": [{"component": "day-link", "props": {"href": bad}}]})


def test_a_prop_value_must_be_the_shape_its_kind_declares() -> None:
    # True is not a number, even though `isinstance(True, int)` is True in Python.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "day-link", "props": {"href": "/d", "count": True}}]})
    assert "number" in str(refused.value)
    assert parse({"blocks": [{"component": "day-link", "props": {"href": "/d", "count": 3}}]})

    # Text is not a number.
    with pytest.raises(SpecError):
        parse({"blocks": [{"component": "day-link", "props": {"href": "/d", "count": "3"}}]})

    # A list prop must be a list.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": "standup"}}]})
    assert "is a list" in str(refused.value)

    # A cell is a value: nested structure is how a spec becomes a document language.
    with pytest.raises(SpecError) as refused:
        parse(
            {
                "blocks": [
                    {"component": "table", "props": {"columns": ["a"], "rows": [[{"deep": "thing"}]]}}
                ]
            }
        )
    assert "nested" in str(refused.value)

    # A row that is not a row is refused with its position.
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "table", "props": {"columns": ["a"], "rows": ["not a row"]}}]})
    assert "rows[0]" in str(refused.value)

    # Booleans and numbers survive as cells.
    spec = parse(
        {"blocks": [{"component": "table", "props": {"columns": ["a", "b"], "rows": [[True, 3]]}}]}
    )
    assert spec.blocks[0].props["rows"] == [[True, 3]]


def test_a_page_is_bounded_and_the_limit_is_named() -> None:
    block = {"component": "list", "props": {"items": ["a"]}}
    with pytest.raises(SpecError) as refused:
        parse({"blocks": [block] * (MAX_BLOCKS + 1)})
    assert str(MAX_BLOCKS) in str(refused.value)

    with pytest.raises(SpecError) as refused:
        parse({"blocks": [{"component": "list", "props": {"items": ["a"] * (MAX_ITEMS + 1)}}]})
    assert str(MAX_ITEMS) in str(refused.value)

    # A value longer than the limit is refused rather than truncated.
    with pytest.raises(SpecError):
        parse({"blocks": [{"component": "list", "props": {"items": ["x" * 5000]}}]})


def test_describe_teaches_the_vocabulary_within_a_stable_text() -> None:
    text = describe(VOCABULARY)
    for name in VOCABULARY:
        assert name in text
    assert "value: text" in text
    assert "note: text?" in text, "an optional prop is not marked as optional"
    assert '"component"' in text
    assert '"props"' in text
    assert "never markup" in text

    # Stable and sorted, so the prompt does not churn between calls.
    assert describe(VOCABULARY) == describe(VOCABULARY)
    assert describe(VOCABULARY).index("- day-link") < describe(VOCABULARY).index("- list")
