"""Theming: a mood is a complete, validated token set — never a stylesheet.

The value of these tests is mostly in what they *forbid*. The dashboard is
generated per render, and it still may never be arbitrary model-generated CSS, so
a token is not free-form style: a colour is hex, a length is a length, and a font
stack may not contain a parenthesis — because a parenthesis is `url(...)`, which
is a fetch, which is the page talking to somewhere the owner did not choose.

The other half is coherence: every mood is complete, every `var()` in the sheet
names a declared token, and the dataclass and the token list agree. A theme with
a hole in it is a theme that gets filled in by a default, and that is how a theme
becomes whatever the fallback happened to be.
"""

from __future__ import annotations

import re

import pytest

from daedalus.genui.theming import (
    BASE_CSS,
    DEFAULT_MOOD,
    MOODS,
    MOOD_NAMES,
    TOKEN_NAMES,
    TokenError,
    Tokens,
    declared_token_names,
    infer,
    resolve,
    sheet,
    tokens_for,
)


# -- completeness ------------------------------------------------------------


def test_the_token_list_and_the_dataclass_agree_exactly() -> None:
    """A token added in one place and forgotten in the other is a hole in every mood."""
    assert set(TOKEN_NAMES) == set(declared_token_names())
    assert len(TOKEN_NAMES) == len(declared_token_names()), "a token name is listed twice"


@pytest.mark.parametrize("mood", MOOD_NAMES)
def test_every_mood_supplies_every_token(mood: str) -> None:
    assert set(MOODS[mood]) == set(TOKEN_NAMES), f"{mood} is not a complete token set"


@pytest.mark.parametrize("mood", MOOD_NAMES)
def test_every_mood_validates(mood: str) -> None:
    tokens = tokens_for(mood)
    assert isinstance(tokens, Tokens)
    for name in TOKEN_NAMES:
        assert getattr(tokens, name), f"{mood}.{name} is empty"


def test_a_mood_missing_a_token_is_refused_rather_than_defaulted() -> None:
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


def test_the_sheet_uses_only_tokens_that_are_declared() -> None:
    """The engine cannot quietly depend on styling that is not the owner's vocabulary."""
    used = set(re.findall(r"var\(--d-([a-z-]+)\)", BASE_CSS))
    assert used, "the sheet reads no tokens at all, which cannot be right"
    assert used <= {name.replace("_", "-") for name in TOKEN_NAMES}


def test_the_sheet_defines_every_token_it_reads() -> None:
    declared = set(re.findall(r"--d-([a-z-]+):", tokens_for("calm").css()))
    used = set(re.findall(r"var\(--d-([a-z-]+)\)", BASE_CSS))
    assert used <= declared


# -- validation: a token is not free-form style ------------------------------


@pytest.mark.parametrize("value", ["red", "rgb(1,2,3)", "#12345", "expression(alert(1))"])
def test_a_colour_that_is_not_hex_is_refused(value: str) -> None:
    MOODS["__test__"] = {**MOODS[DEFAULT_MOOD], "accent": value}
    try:
        with pytest.raises(TokenError):
            tokens_for("__test__")
    finally:
        del MOODS["__test__"]


@pytest.mark.parametrize("value", ["15", "calc(1px + 2px)", "1px; } body { display:none", "auto"])
def test_a_length_that_is_not_a_length_is_refused(value: str) -> None:
    MOODS["__test__"] = {**MOODS[DEFAULT_MOOD], "gap": value}
    try:
        with pytest.raises(TokenError):
            tokens_for("__test__")
    finally:
        del MOODS["__test__"]


def test_a_font_stack_that_could_open_a_function_is_refused() -> None:
    """`url(...)` rides in on a font. This is an allowlist, not a style preference."""
    for value in ("url(https://x/y.woff)", "system-ui; @import url(x)", "a\\b", "x:y"):
        MOODS["__test__"] = {**MOODS[DEFAULT_MOOD], "font": value}
        try:
            with pytest.raises(TokenError):
                tokens_for("__test__")
        finally:
            del MOODS["__test__"]


def test_an_ordinary_font_stack_is_accepted() -> None:
    assert tokens_for("calm").font == "system-ui, sans-serif"
    assert tokens_for("low-energy").font == "Georgia, serif"


def test_a_motion_value_that_is_not_a_duration_is_refused() -> None:
    MOODS["__test__"] = {**MOODS[DEFAULT_MOOD], "motion": "ease-in-out"}
    try:
        with pytest.raises(TokenError):
            tokens_for("__test__")
    finally:
        del MOODS["__test__"]


# -- mood resolution --------------------------------------------------------


def test_an_unknown_mood_falls_back_to_calm_rather_than_rendering_unstyled() -> None:
    assert tokens_for("nonsense") == tokens_for(DEFAULT_MOOD)
    assert tokens_for("") == tokens_for(DEFAULT_MOOD)
    assert tokens_for("CALM") == tokens_for(DEFAULT_MOOD)


def test_the_owner_override_beats_the_inferred_mood() -> None:
    assert resolve(inferred="dense-ops", override="celebration") == tokens_for("celebration")


def test_the_inferred_mood_is_used_when_there_is_no_override() -> None:
    assert resolve(inferred="midnight-focus") == tokens_for("midnight-focus")


def test_an_unusable_override_falls_through_to_the_inferred_mood() -> None:
    """A typo in an override should not cost the owner their mood."""
    assert resolve(inferred="dense-ops", override="vibes") == tokens_for("dense-ops")
    assert resolve(inferred="vibes", override="") == tokens_for(DEFAULT_MOOD)


# -- inference from structured signals --------------------------------------


def test_an_ordinary_afternoon_is_calm() -> None:
    assert infer(hour=14) == "calm"


def test_a_crowded_day_reads_as_dense_ops() -> None:
    assert infer(hour=14, busy_blocks=4) == "dense-ops"
    assert infer(hour=14, busy_blocks=3) == "calm"


@pytest.mark.parametrize("hour", [21, 22, 23, 0, 3, 5])
def test_a_late_or_early_hour_reads_as_midnight_focus(hour: int) -> None:
    assert infer(hour=hour) == "midnight-focus"


@pytest.mark.parametrize("hour", [6, 12, 20])
def test_the_working_day_is_not_midnight(hour: int) -> None:
    assert infer(hour=hour) != "midnight-focus"


def test_a_flat_battery_outranks_a_late_hour() -> None:
    assert infer(hour=23, draining=True) == "low-energy"
    assert infer(hour=23, draining=False) == "midnight-focus"


def test_a_celebration_outranks_everything() -> None:
    assert infer(hour=23, draining=True, busy_blocks=9, celebrating=True) == "celebration"


def test_inference_never_reads_a_clock_itself() -> None:
    """The hour is passed in — the caller owns the zone, nothing here guesses one."""
    import inspect

    signature = inspect.signature(infer)
    assert "hour" in signature.parameters
    assert signature.parameters["hour"].kind is inspect.Parameter.KEYWORD_ONLY


# -- the sheet ---------------------------------------------------------------


def test_the_sheet_is_the_tokens_then_the_components() -> None:
    css = sheet(tokens_for("calm"))
    assert css.startswith(":root {")
    assert "--d-accent: #2f5fd0;" in css
    assert ".calendar__day--today" in css


def test_every_mood_produces_a_different_sheet() -> None:
    sheets = {sheet(tokens_for(mood)) for mood in MOOD_NAMES}
    assert len(sheets) == len(MOOD_NAMES), "two moods render identically"


def test_no_mood_sheet_can_reach_out_to_the_network() -> None:
    """Self-contained is the promise; a fetch in the theme would break it silently."""
    for mood in MOOD_NAMES:
        css = sheet(tokens_for(mood))
        assert "url(" not in css, f"{mood} fetches something"
        assert "@import" not in css, f"{mood} imports something"
