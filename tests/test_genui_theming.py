"""Theming: a mood is a complete, validated token set — never a stylesheet.

A token is not free-form style: a colour is hex, a length is a length, and a font
stack may not contain a parenthesis — because a parenthesis is `url(...)`, which is
a fetch, which is the page talking to somewhere the owner did not choose. The other
half is coherence: every mood is complete, every `var()` in the sheet names a
declared token, and a mood with a hole is refused rather than filled in by a default.
"""

from __future__ import annotations

import inspect
import re

import pytest

from daedalus.genui.theming import (
    BASE_CSS,
    DEFAULT_MOOD,
    MOODS,
    MOOD_NAMES,
    TOKEN_NAMES,
    TokenError,
    declared_token_names,
    infer,
    resolve,
    sheet,
    tokens_for,
)


def test_the_token_list_and_the_dataclass_agree_exactly() -> None:
    """A token added in one place and forgotten in the other is a hole in every mood."""
    assert set(TOKEN_NAMES) == set(declared_token_names())
    assert len(TOKEN_NAMES) == len(declared_token_names()), "a token name is listed twice"


def test_a_mood_is_complete_or_it_is_refused_rather_than_defaulted() -> None:
    for mood in MOOD_NAMES:
        tokens = tokens_for(mood)
        assert set(MOODS[mood]) == set(TOKEN_NAMES), f"{mood} is not a complete token set"
        for name in TOKEN_NAMES:
            assert getattr(tokens, name), f"{mood}.{name} is empty"

    # A mood with one token deleted is refused, and the refusal names the hole.
    incomplete = dict(MOODS[DEFAULT_MOOD])
    del incomplete["alert"]
    MOODS["__test__"] = incomplete
    try:
        with pytest.raises(TokenError) as refused:
            tokens_for("__test__")
        assert "alert" in str(refused.value)
        assert "complete" in str(refused.value)
    finally:
        del MOODS["__test__"]


def test_the_sheet_reads_only_declared_tokens_and_defines_every_one_it_reads() -> None:
    """The engine cannot quietly depend on styling that is not the owner's vocabulary."""
    used = set(re.findall(r"var\(--d-([a-z-]+)\)", BASE_CSS))
    assert used, "the sheet reads no tokens at all, which cannot be right"
    assert used <= {name.replace("_", "-") for name in TOKEN_NAMES}

    declared = set(re.findall(r"--d-([a-z-]+):", tokens_for("calm").css()))
    assert used <= declared


def test_a_token_value_is_checked_against_its_own_shape() -> None:
    """A token is never free-form style, whether it is a colour, a length or a time."""
    bad = [
        # a colour is hex, six or three digits, and nothing else
        ("accent", "red"),
        ("accent", "rgb(1,2,3)"),
        ("accent", "#12345"),
        ("accent", "expression(alert(1))"),
        # a length is a length, so it cannot close a rule or call a function
        ("gap", "15"),
        ("gap", "calc(1px + 2px)"),
        ("gap", "1px; } body { display:none"),
        ("gap", "auto"),
        # motion is a duration
        ("motion", "ease-in-out"),
        # calc() belongs in the *sheet*; a calc() in a token value stays refused
        ("radius", "calc(4px + 1px)"),
    ]
    for name, value in bad:
        MOODS["__test__"] = {**MOODS[DEFAULT_MOOD], name: value}
        try:
            with pytest.raises(TokenError):
                tokens_for("__test__")
        finally:
            del MOODS["__test__"]


def test_a_font_stack_may_not_open_a_function_but_an_ordinary_one_is_accepted() -> None:
    """`url(...)` rides in on a font. This is an allowlist, not a style preference."""
    for value in ("url(https://x/y.woff)", "system-ui; @import url(x)", "a\\b", "x:y"):
        MOODS["__test__"] = {**MOODS[DEFAULT_MOOD], "font": value}
        try:
            with pytest.raises(TokenError):
                tokens_for("__test__")
        finally:
            del MOODS["__test__"]

    assert tokens_for("calm").font == "system-ui, sans-serif"
    assert tokens_for("low-energy").font == "Georgia, serif"


def test_mood_resolution_prefers_the_override_then_the_inferred_mood_then_calm() -> None:
    # An unknown mood name falls back to calm rather than rendering unstyled.
    assert tokens_for("nonsense") == tokens_for(DEFAULT_MOOD)
    assert tokens_for("") == tokens_for(DEFAULT_MOOD)
    assert tokens_for("CALM") == tokens_for(DEFAULT_MOOD)

    # The owner's one word beats the inferred mood, and the inferred mood is used
    # when there is no override.
    assert resolve(inferred="dense-ops", override="celebration") == tokens_for("celebration")
    assert resolve(inferred="midnight-focus") == tokens_for("midnight-focus")

    # A typo in an override should not cost the owner their mood.
    assert resolve(inferred="dense-ops", override="vibes") == tokens_for("dense-ops")
    assert resolve(inferred="vibes", override="") == tokens_for(DEFAULT_MOOD)


def test_inference_reads_structured_signals_in_a_fixed_priority_order() -> None:
    # An ordinary afternoon is calm; a crowded day reads as dense-ops.
    assert infer(hour=14) == "calm"
    assert infer(hour=14, busy_blocks=4) == "dense-ops"
    assert infer(hour=14, busy_blocks=3) == "calm"

    # A late or early hour reads as midnight-focus, and the working day does not.
    for hour in (21, 22, 23, 0, 3, 5):
        assert infer(hour=hour) == "midnight-focus"
    for hour in (6, 12, 20):
        assert infer(hour=hour) != "midnight-focus"

    # A flat battery outranks a late hour, and a celebration outranks everything.
    assert infer(hour=23, draining=True) == "low-energy"
    assert infer(hour=23, draining=False) == "midnight-focus"
    assert infer(hour=23, draining=True, busy_blocks=9, celebrating=True) == "celebration"

    # The hour is passed in — the caller owns the zone, nothing here guesses one.
    assert "hour" in inspect.signature(infer).parameters


def test_the_sheet_is_the_tokens_then_the_components_and_every_mood_differs() -> None:
    calm = tokens_for("calm")
    css = sheet(calm)
    assert css.startswith(":root {")
    assert f"--d-accent: {calm.accent};" in css
    assert ".calendar__day--today" in css

    sheets = {sheet(tokens_for(mood)) for mood in MOOD_NAMES}
    assert len(sheets) == len(MOOD_NAMES), "two moods render identically"


def test_no_mood_sheet_can_reach_out_to_the_network() -> None:
    """Self-contained is the promise; a fetch in the theme would break it silently."""
    for mood in MOOD_NAMES:
        css = sheet(tokens_for(mood))
        assert "url(" not in css, f"{mood} fetches something"
        assert "@import" not in css, f"{mood} imports something"


def test_the_scale_is_carried_derived_and_scheme_composed() -> None:
    """Spacing, radius, type and shadow are one formula over declared tokens."""
    for mood in MOOD_NAMES:
        css = sheet(tokens_for(mood))
        for primitive in (
            "--d-space-3: var(--d-gap);",
            "--d-radius-lg: calc(var(--d-radius) * 1.5);",
            "--d-type-lg: calc(var(--d-size) * 1.25);",
            "--d-ring: 0 0 0 3px color-mix(",
        ):
            assert primitive in css, f"{mood}'s sheet lost {primitive.split(':')[0]}"

    # One formula, no per-mood code: the scale reads the gap token, so the number
    # the owner's mood chose is the number the scale composes.
    calm = [line for line in sheet(tokens_for("calm")).splitlines() if "--d-space-4:" in line]
    dense = [line for line in sheet(tokens_for("dense-ops")).splitlines() if "--d-space-4:" in line]
    assert calm == dense
    assert "var(--d-gap)" in calm[0]

    # A shadow is composed from the scheme, not one size for every mood.
    for dark_mood in ("midnight-focus", "celebration"):
        dark = sheet(tokens_for(dark_mood))
        for light_mood in ("calm", "low-energy"):
            light = sheet(tokens_for(light_mood))
            dark_1 = next(line for line in dark.splitlines() if "--d-shadow-1:" in line)
            light_1 = next(line for line in light.splitlines() if "--d-shadow-1:" in line)
            assert dark_1 != light_1, (
                f"{dark_mood} and {light_mood} share a shadow — a shadow is "
                "composed from the scheme, not one number for every mood"
            )
