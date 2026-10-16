"""The UI spec: what may be proposed, and what may not (SPEC §3.6).

The whole design is a split, and this module is where the split is enforced:

    the model composes *structure*; a deterministic renderer *disposes*.

So the model never writes markup, CSS or JS — it fills in a spec assembled from a
vocabulary the owner approved: named components, each with typed props. That only
stays true if something *checks* a proposal before anything renders, refusing the
unknown component, the unknown prop, the missing one, and the value that is
secretly a `<script>` tag. This is that check.

**Two defences, not one.** A value carrying a tag is refused here, at the door,
and escaped again at render time, at the last mile. The door is what keeps the
vocabulary honest; the last mile is what keeps a bug in a single component from
becoming an injection. Neither alone is a design — the pair is.

**Refusals name the way out.** A message that says only *"bad prop"* sends the
model hunting, and a model that is hunting retries the same call. Every refusal
here names what it received and what it would have accepted instead, because the
next step should be one step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

#: Bounded on purpose: a spec is a page the owner opens, not a way to make one.
MAX_BLOCKS = 24
MAX_ITEMS = 60
MAX_CELLS = 12
MAX_TEXT = 600
MAX_TITLE = 120
MAX_MOOD = 24

#: A tag, not a less-than sign — "dinner <-> party" is a value, "<b>" is markup.
_TAG = re.compile(r"<\s*/?\s*[A-Za-z!]")
_SCHEME = re.compile(r"\b(?:javascript|data|vbscript|file)\s*:", re.IGNORECASE)
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")


class SpecError(ValueError):
    """A proposed spec cannot be rendered as given. Refused, never guessed at."""


class Kind(str, Enum):
    """What a prop can hold. Deliberately a small, closed set."""

    TEXT = "text"
    LINK = "link"
    NUMBER = "number"
    BOOL = "bool"
    LIST = "list"
    ROWS = "rows"


@dataclass(frozen=True, slots=True)
class Prop:
    kind: Kind = Kind.TEXT
    required: bool = False
    help: str = ""


@dataclass(frozen=True, slots=True)
class Component:
    """One entry in the vocabulary: a name, what it is for, and its props."""

    name: str
    summary: str
    props: Mapping[str, Prop] = field(default_factory=dict)

    def required(self) -> tuple[str, ...]:
        return tuple(key for key, prop in self.props.items() if prop.required)


@dataclass(frozen=True, slots=True)
class Block:
    """One component, with the values it was given."""

    component: str
    props: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"component": self.component, "props": dict(self.props)}


@dataclass(frozen=True, slots=True)
class Spec:
    """A whole page: an ordered list of blocks, and how they should feel."""

    blocks: tuple[Block, ...] = ()
    title: str = ""
    mood: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "mood": self.mood,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    @classmethod
    def parse(cls, raw: Any, *, vocabulary: Mapping[str, Component]) -> "Spec":
        """Validate a proposal against the vocabulary, or refuse it.

        Accepts the two spellings a model reaches for — a block's ``component`` or
        ``type``, and a page's ``blocks`` or ``components`` — because a refusal
        caused by a synonym is a refusal that teaches nothing.
        """
        if not isinstance(raw, Mapping):
            raise SpecError(
                f"a spec is an object with `blocks`; I received {_typename(raw)}"
            )
        unknown = sorted(set(raw) - {"blocks", "components", "title", "mood"})
        if unknown:
            raise SpecError(
                f"unknown key{'s' if len(unknown) > 1 else ''} {', '.join(map(repr, unknown))} "
                "at the top level; a spec has `blocks`, `title` and `mood`"
            )
        blocks_raw = raw.get("blocks", raw.get("components", []))
        if isinstance(blocks_raw, Mapping):
            blocks_raw = [blocks_raw]
        if not isinstance(blocks_raw, (list, tuple)):
            raise SpecError(f"`blocks` is a list; I received {_typename(blocks_raw)}")
        if len(blocks_raw) > MAX_BLOCKS:
            raise SpecError(f"{len(blocks_raw)} blocks; a page holds at most {MAX_BLOCKS}")
        blocks = tuple(
            _parse_block(entry, index, vocabulary) for index, entry in enumerate(blocks_raw)
        )
        title = _text(raw.get("title", ""), "title", MAX_TITLE) if raw.get("title") else ""
        mood = _text(raw.get("mood", ""), "mood", MAX_MOOD) if raw.get("mood") else ""
        return cls(blocks=blocks, title=title, mood=mood)


def describe(vocabulary: Mapping[str, Component]) -> str:
    """The vocabulary, written for the model — the only components it may use.

    `?` marks a prop that may be omitted; everything else is required.
    """
    lines = [
        "A dashboard is a list of blocks. Each block is "
        '{"component": "<name>", "props": {…}}.',
        "Values are text, never markup: no tags, no scripts, and the only link is a "
        "relative path.",
        "",
        "Components (use these names exactly):",
    ]
    for name in sorted(vocabulary):
        component = vocabulary[name]
        props = ", ".join(
            f"{key}: {prop.kind.value}{'' if prop.required else '?'}"
            for key, prop in component.props.items()
        )
        lines.append(f"- {name}({props}) — {component.summary}")
    return "\n".join(lines)


# -- parsing -----------------------------------------------------------------


def _parse_block(entry: Any, index: int, vocabulary: Mapping[str, Component]) -> Block:
    where = f"block {index + 1}"
    if not isinstance(entry, Mapping):
        raise SpecError(f"{where} is an object with `component` and `props`; it is {_typename(entry)}")
    name = entry.get("component", entry.get("type", ""))
    if not isinstance(name, str) or not name.strip():
        raise SpecError(
            f"{where} needs a `component`; it received "
            f"{', '.join(map(repr, entry)) or 'nothing'}. The vocabulary is: "
            f"{', '.join(sorted(vocabulary))}"
        )
    name = name.strip()
    component = vocabulary.get(name)
    if component is None:
        raise SpecError(
            f"{where}: {name!r} is not a component. The vocabulary is: "
            f"{', '.join(sorted(vocabulary))}"
        )
    props_raw = entry.get("props", {})
    if props_raw is None:
        props_raw = {}
    if not isinstance(props_raw, Mapping):
        raise SpecError(f"{where} ({name}): `props` is an object; it is {_typename(props_raw)}")
    extra = sorted(set(props_raw) - set(component.props))
    if extra:
        raise SpecError(
            f"{where} ({name}): {', '.join(map(repr, extra))} "
            f"{'are not props' if len(extra) > 1 else 'is not a prop'} of {name}. "
            f"It takes: {', '.join(component.props) or 'nothing'}"
        )
    missing = [key for key in component.required() if key not in props_raw]
    if missing:
        raise SpecError(
            f"{where} ({name}) is missing {', '.join(map(repr, missing))}. "
            f"{name} takes: {', '.join(component.props)}"
        )
    props: dict[str, Any] = {}
    for key, value in props_raw.items():
        prop = component.props[key]
        props[key] = _coerce(prop.kind, value, f"{where} ({name}).{key}")
    return Block(component=name, props=props)


def _coerce(kind: Kind, value: Any, where: str) -> Any:
    if kind is Kind.TEXT:
        return _text(value, where)
    if kind is Kind.LINK:
        return _link(value, where)
    if kind is Kind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(f"{where} is a number; it is {_typename(value)}")
        return value
    if kind is Kind.BOOL:
        if not isinstance(value, bool):
            raise SpecError(f"{where} is true or false; it is {_typename(value)}")
        return value
    if kind is Kind.LIST:
        return _items(value, where)
    if kind is Kind.ROWS:
        return _rows(value, where)
    raise SpecError(f"{where}: unknown kind {kind!r}")  # pragma: no cover — closed enum


def _items(value: Any, where: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise SpecError(f"{where} is a list; it is {_typename(value)}")
    if len(value) > MAX_ITEMS:
        raise SpecError(f"{where} holds {len(value)} items; the limit is {MAX_ITEMS}")
    return [_scalar(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _rows(value: Any, where: str) -> list[list[Any]]:
    if not isinstance(value, (list, tuple)):
        raise SpecError(f"{where} is a list of rows; it is {_typename(value)}")
    if len(value) > MAX_ITEMS:
        raise SpecError(f"{where} holds {len(value)} rows; the limit is {MAX_ITEMS}")
    rows: list[list[Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, (list, tuple)):
            raise SpecError(f"{where}[{index}] is a row (a list of cells); it is {_typename(row)}")
        if len(row) > MAX_CELLS:
            raise SpecError(f"{where}[{index}] holds {len(row)} cells; the limit is {MAX_CELLS}")
        rows.append([_scalar(cell, f"{where}[{index}]") for cell in row])
    return rows


def _scalar(value: Any, where: str) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _text(value, where)
    raise SpecError(
        f"{where} is text or a number; it is {_typename(value)}. "
        "A cell is a value, never a nested structure"
    )


def _text(value: Any, where: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{where} is text; it is {_typename(value)}")
    text = value.strip()
    if len(text) > limit:
        raise SpecError(f"{where} is {len(text)} characters; the limit is {limit}")
    if _TAG.search(text):
        raise SpecError(
            f"{where} contains markup ({_TAG.search(text).group()!r}). "
            "Values are text — the renderer draws the markup, not the model"
        )
    scheme = _SCHEME.search(text)
    if scheme:
        raise SpecError(f"{where} carries a {scheme.group().strip(':')}: URI; that is not a value")
    return text


def _link(value: Any, where: str) -> str:
    """A link is a path inside this server, and nothing else.

    Not a style rule — an allowlist. An absolute URL in a spec would make the
    rendered page fetch something the owner never chose, and a `javascript:` URL
    would make a *display* into an action.
    """
    text = _text(value, where)
    if not text.startswith("/") or text.startswith("//"):
        raise SpecError(f"{where} is a path on this server starting with `/`; it is {value!r}")
    if ":" in text.split("?", 1)[0]:
        raise SpecError(f"{where} is a plain path, not a URL with a scheme; it is {value!r}")
    return text


def _typename(value: Any) -> str:
    if value is None:
        return "nothing (null)"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, str):
        return f"the text {value[:40]!r}"
    if isinstance(value, Mapping):
        return f"an object with {', '.join(map(repr, value)) or 'no keys'}"
    if isinstance(value, (list, tuple)):
        return f"a list of {len(value)}"
    if isinstance(value, (int, float)):
        return f"the number {value}"
    return type(value).__name__
