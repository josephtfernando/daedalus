"""Theming: validated tokens, and how a mood becomes one (SPEC §3.6).

The dashboard is *generated per render*, "in the owner's theme, for the owner's
mood" — and it still may never be arbitrary model-generated CSS. So the theme is
not a stylesheet the model writes; it is a **token set** the owner approved, and a
mood is a *named choice among those sets*.

Three rules make that hold:

1. **A mood is complete or it does not exist.** Every mood supplies every token.
   A hole would have to be filled by a default somewhere, and "a default somewhere"
   is how a theme drifts into being whatever the fallback happened to be.
2. **Every value is validated.** Colours are hex, lengths are lengths, and a font
   stack may not contain a parenthesis — so `url(…)` cannot ride in on a font, and
   no token can close a rule and start writing its own CSS.
3. **All CSS lives here, and it only reads declared tokens.** The component sheet
   below is the single place styling is written, and a test asserts every
   `var(--d-…)` it uses is one of the tokens — so a component cannot quietly depend
   on styling that is not part of the owner's vocabulary.

Mood is **inferred from structured local signals**, never from a model, with a
one-word owner override that wins. An unrecognised mood falls back to `calm`
rather than rendering unstyled: the failure mode of a bad mood name should be a
plain page, not a broken one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from typing import Mapping

__all__ = [
    "BASE_CSS",
    "DEFAULT_MOOD",
    "MOODS",
    "MOOD_NAMES",
    "TOKEN_NAMES",
    "TokenError",
    "Tokens",
    "apply",
    "check_token",
    "infer",
    "resolve",
    "sheet",
    "tokens_for",
]

#: The token names a sheet may reference, in the order they are declared.
TOKEN_NAMES: tuple[str, ...] = (
    "bg",
    "surface",
    "text",
    "muted",
    "accent",
    "accent_text",
    "border",
    "alert",
    "font",
    "size",
    "leading",
    "gap",
    "radius",
    "motion",
    "scheme",
)

_COLOUR = frozenset(
    {"bg", "surface", "text", "muted", "accent", "accent_text", "border", "alert"}
)
_LENGTH = frozenset({"size", "gap", "radius"})
#: `line-height` is a plain number, and motion is a duration.
_NUMBER = frozenset({"leading"})
_TIME = frozenset({"motion"})
#: A closed set, like `Prop.choices`: the browser's light/dark decision is the
#: theme's, and a dark page with light scrollbars reads as broken.
_CHOICE = {"scheme": ("light", "dark")}

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
#: A length needs a unit — "0" is the only one CSS accepts bare, and it does accept it.
_LENGTH_VALUE = re.compile(r"^(?:0|\d+(?:\.\d+)?(?:px|rem|em|%))$")
_NUMBER_VALUE = re.compile(r"^\d+(?:\.\d+)?$")
_TIME_VALUE = re.compile(r"^\d+(?:\.\d+)?(?:ms|s)?$")
#: A font stack: names, commas, quotes, hyphens. Nothing that can open a function
#: or close a rule — so `url(…)`, `);}`, and `@import` are not available.
_FONT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,'\"-]*$")
_FONT_FORBIDDEN = ("(", ")", ";", "{", "}", "\\", "/", "@", ":", "!")

DEFAULT_MOOD = "calm"


class TokenError(ValueError):
    """A token set is not usable. Refused, because an invalid token is invalid CSS."""


@dataclass(frozen=True, slots=True)
class Tokens:
    """A complete theme. Every field is required, which is the point."""

    bg: str
    surface: str
    text: str
    muted: str
    accent: str
    accent_text: str
    border: str
    alert: str
    font: str
    size: str
    leading: str
    gap: str
    radius: str
    motion: str
    scheme: str

    def css(self) -> str:
        """The token set as custom properties — the only bridge to the sheet."""
        lines = [
            f"  --d-{name.replace('_', '-')}: {getattr(self, name)};"
            for name in TOKEN_NAMES
        ]
        return ":root {\n" + "\n".join(lines) + "\n}"


#: Every mood supplies every token. See rule 1 in the module docstring.
MOODS: dict[str, dict[str, str]] = {
    "calm": {
        "bg": "#f7f7f5",
        "surface": "#ffffff",
        "text": "#1c1c1e",
        "muted": "#6b6b70",
        "accent": "#2f5fd0",
        "accent_text": "#f7f7f5",
        "border": "#dcdcd6",
        "alert": "#a3341f",
        "font": "system-ui, sans-serif",
        "size": "15px",
        "leading": "1.5",
        "gap": "16px",
        "radius": "10px",
        "motion": "120ms",
        "scheme": "light",
    },
    "midnight-focus": {
        "bg": "#0d0e12",
        "surface": "#16181f",
        "text": "#e7e9ee",
        "muted": "#8b90a0",
        "accent": "#7aa2f7",
        "accent_text": "#0d0e12",
        "border": "#262a35",
        "alert": "#f7768e",
        "font": "ui-monospace, SFMono-Regular, monospace",
        "size": "15px",
        "leading": "1.6",
        "gap": "18px",
        "radius": "8px",
        "motion": "100ms",
        "scheme": "dark",
    },
    "dense-ops": {
        "bg": "#12141a",
        "surface": "#1a1d25",
        "text": "#e3e6ec",
        "muted": "#8f95a3",
        "accent": "#5b9bd5",
        "accent_text": "#12141a",
        "border": "#272b36",
        "alert": "#d98c5f",
        "font": "ui-monospace, SFMono-Regular, monospace",
        "size": "13px",
        "leading": "1.35",
        "gap": "8px",
        "radius": "6px",
        "motion": "0ms",
        "scheme": "dark",
    },
    "low-energy": {
        "bg": "#f6f2ec",
        "surface": "#fffaf3",
        "text": "#33302b",
        "muted": "#7d776d",
        "accent": "#8a7a5c",
        "accent_text": "#fffaf3",
        "border": "#e2dbd0",
        "alert": "#9c6b4a",
        "font": "Georgia, serif",
        "size": "17px",
        "leading": "1.7",
        "gap": "22px",
        "radius": "14px",
        "motion": "200ms",
        "scheme": "light",
    },
    "celebration": {
        "bg": "#1b1030",
        "surface": "#2a1a49",
        "text": "#f6f0ff",
        "muted": "#b8a8d9",
        "accent": "#ffd166",
        "accent_text": "#1b1030",
        "border": "#3d2a63",
        "alert": "#ff7b9c",
        "font": "system-ui, sans-serif",
        "size": "16px",
        "leading": "1.55",
        "gap": "18px",
        "radius": "16px",
        "motion": "260ms",
        "scheme": "dark",
    },
}

MOOD_NAMES: tuple[str, ...] = tuple(MOODS)


# -- validation --------------------------------------------------------------


def _check(name: str, value: str, mood: str) -> None:
    where = f"mood {mood!r}, token {name!r}"
    if not isinstance(value, str) or not value:
        raise TokenError(f"{where} is empty")
    if name in _COLOUR and not _HEX.match(value):
        raise TokenError(f"{where} is a hex colour (#rrggbb); it is {value!r}")
    if name in _LENGTH and not _LENGTH_VALUE.match(value):
        raise TokenError(f"{where} is a length (12px, 1rem); it is {value!r}")
    if name in _NUMBER and not _NUMBER_VALUE.match(value):
        raise TokenError(f"{where} is a plain number; it is {value!r}")
    if name in _TIME and not _TIME_VALUE.match(value):
        raise TokenError(f"{where} is a duration (120ms, 0.2s); it is {value!r}")
    if name in _CHOICE and value not in _CHOICE[name]:
        raise TokenError(f"{where} is one of: {' | '.join(_CHOICE[name])}; it is {value!r}")
    if name == "font":
        if not _FONT.match(value):
            raise TokenError(f"{where} is a font stack; it is {value!r}")
        bad = [part for part in _FONT_FORBIDDEN if part in value]
        if bad:
            # Not a style rule — an allowlist. A font stack that can open a
            # parenthesis can carry `url(...)`, and then the page fetches.
            raise TokenError(f"{where} may not contain {' '.join(map(repr, bad))}: {value!r}")


def tokens_for(mood: str) -> Tokens:
    """The token set for a mood, validated. An unknown mood is `calm`, not an error."""
    name = str(mood or "").strip().lower()
    if name not in MOODS:
        name = DEFAULT_MOOD
    values = MOODS[name]
    missing = [token for token in TOKEN_NAMES if token not in values]
    if missing:
        raise TokenError(f"mood {name!r} is missing {', '.join(missing)} — a mood is complete")
    for token in TOKEN_NAMES:
        _check(token, values[token], name)
    return Tokens(**{token: values[token] for token in TOKEN_NAMES})


def check_token(name: str, value: str) -> None:
    """One tuned value, against the same rules a mood's own values obey.

    The door for a *tune*: a value that could not render must not even reach the
    pending set, let alone an approval.
    """
    _check(name, value, "tune")


def apply(tokens: Tokens, overrides: Mapping[str, str]) -> Tokens:
    """The owner's adopted values, laid over a mood — and validated again.

    The second check is not ceremony: `approved.toml` is a file the owner edits
    by hand, and a hand-edited value gets exactly the door a proposed one did.
    """
    if not overrides:
        return tokens
    unknown = sorted(set(overrides) - set(TOKEN_NAMES))
    if unknown:
        raise TokenError(
            f"unknown token{'' if len(unknown) == 1 else 's'} {', '.join(map(repr, unknown))} — "
            f"a theme has: {', '.join(TOKEN_NAMES)}"
        )
    values = {name: str(overrides[name]) for name in TOKEN_NAMES if name in overrides}
    for name, value in values.items():
        _check(name, value, "the approved theme")
    return replace(tokens, **values)


def resolve(
    *,
    inferred: str = "",
    override: str = "",
    overrides: Mapping[str, str] | None = None,
) -> Tokens:
    """The owner's override wins; otherwise the inferred mood; otherwise `calm`.

    `overrides` is what the owner has **approved** since — adopted values that
    sit on top of whichever mood is chosen, so a tune can move one colour
    without forking a whole mood.
    """
    chosen = str(override or "").strip().lower()
    if chosen not in MOODS:
        chosen = str(inferred or "").strip().lower()
    return apply(tokens_for(chosen), overrides or {})


# -- inference ---------------------------------------------------------------


def infer(
    *,
    hour: int,
    busy_blocks: int = 0,
    draining: bool = False,
    celebrating: bool = False,
) -> str:
    """A mood from structured local signals — no model, and nothing remote.

    Ordered deliberately: a celebration outranks a flat battery, a flat battery
    outranks a late hour, and a crowded day outranks the default. `hour` is the
    owner's own wall clock; nothing here reads a clock itself.
    """
    if celebrating:
        return "celebration"
    if draining:
        return "low-energy"
    if hour >= 21 or hour < 6:
        return "midnight-focus"
    if busy_blocks >= 4:
        return "dense-ops"
    return DEFAULT_MOOD


# -- the sheet ---------------------------------------------------------------

#: The one place CSS is written. It reads declared tokens and nothing else.
BASE_CSS = """html { color-scheme: var(--d-scheme); }

*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  padding: var(--d-gap);
  background: var(--d-bg);
  color: var(--d-text);
  font-family: var(--d-font);
  font-size: var(--d-size);
  line-height: var(--d-leading);
  -webkit-text-size-adjust: 100%;
}

.dashboard { display: grid; gap: var(--d-gap); max-width: 64rem; margin: 0 auto; }
.dashboard__title { font-size: 1.1em; font-weight: 600; margin: 0; color: var(--d-muted); }

.stat-card, .prose, .timeline, .table, .calendar {
  background: var(--d-surface);
  border: 1px solid var(--d-border);
  border-radius: var(--d-radius);
  padding: var(--d-gap);
  transition: background var(--d-motion) ease, border-color var(--d-motion) ease;
}

.stat-card__label { margin: 0; color: var(--d-muted); font-size: 0.85em; letter-spacing: 0.02em; }
.stat-card__value { margin: 0.25em 0 0; font-size: 1.9em; font-weight: 600; }
.stat-card__note { margin: 0.35em 0 0; color: var(--d-muted); font-size: 0.9em; }

.list { list-style: none; background: var(--d-surface); border: 1px solid var(--d-border);
        border-radius: var(--d-radius); padding: var(--d-gap); margin: 0; }
.list__item { padding: 0.2em 0; }
.list__item + .list__item { border-top: 1px solid var(--d-border); }
.list__empty, .timeline__empty { color: var(--d-muted); margin: 0; }

.timeline { list-style: none; margin: 0; display: grid; gap: 0.35em; }
.timeline__entry { display: grid; grid-template-columns: 6.5em 1fr; gap: var(--d-gap); }
.timeline__when { color: var(--d-muted); font-variant-numeric: tabular-nums; }

.table { width: 100%; border-collapse: collapse; }
.table th { text-align: left; color: var(--d-muted); font-weight: 600;
            font-size: 0.85em; padding: 0.3em 0.6em 0.3em 0; }
.table td { padding: 0.3em 0.6em 0.3em 0; border-top: 1px solid var(--d-border); }

.prose__heading { margin: 0 0 0.4em; font-size: 1em; }
.prose p { margin: 0; }

.calendar__days { list-style: none; margin: 0; padding: 0; display: grid;
                  grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0.4em; }
.calendar--day .calendar__days { grid-template-columns: 1fr; }
.calendar__day { display: grid; gap: 0.15em; padding: 0.4em;
                 border: 1px solid var(--d-border); border-radius: var(--d-radius); }
.calendar__date { font-variant-numeric: tabular-nums; font-weight: 600; }
.calendar__weekday { color: var(--d-muted); font-size: 0.8em; }
.calendar__what { font-size: 0.85em; }
.calendar__day--today { border-color: var(--d-accent); box-shadow: inset 0 0 0 1px var(--d-accent); }
.calendar__day--today .calendar__date { color: var(--d-accent); }

@media (max-width: 34rem) {
  .calendar__days { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .calendar--day .calendar__days { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
"""


def sheet(tokens: Tokens) -> str:
    """The complete stylesheet: the token set, then the components that read it."""
    return tokens.css() + "\n\n" + BASE_CSS


def declared_token_names() -> tuple[str, ...]:
    """The dataclass's own field order — checked against `TOKEN_NAMES` in tests."""
    return tuple(field.name for field in fields(Tokens))
