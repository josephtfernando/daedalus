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
from typing import Any, Callable, Mapping

#: Bounded on purpose: a spec is a page the owner opens, not a way to make one.
MAX_BLOCKS = 24
MAX_ITEMS = 60
MAX_CELLS = 12
MAX_TEXT = 600
MAX_TITLE = 120
MAX_MOOD = 24

#: A tag, not a less-than sign — "dinner <-> party" is a value, "<b>" is markup.
_TAG = re.compile(r"<\s*/?\s*[A-Za-z!]")
#: A dotted, lower-case key: `disk.free`, `calendar.today.events`, `body.sleep`.
_KEY = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
_SCHEME = re.compile(r"\b(?:javascript|data|vbscript|file)\s*:", re.IGNORECASE)
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")


class SpecError(ValueError):
    """A proposed spec cannot be rendered as given. Refused, never guessed at."""


@dataclass(frozen=True, slots=True)
class Binding:
    """A value the model does **not** get to see.

    This is the whole of "structure travels, values stay local" (§3.6, §6). The
    model composes a page out of *keys* — `{"bind": "disk.free"}` — and a local
    lookup fills them in at render time. A `local_only` fact can therefore appear
    on the owner's own dashboard without ever having been in a prompt, not because
    something remembered to filter it, but because it was never in the spec at all.
    """

    key: str


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
    #: For TEXT and LINK: the only values accepted, in order. A closed set that is
    #: written down is a closed set a model can hit first time; one that is only
    #: checked by a renderer is a refusal the model discovers by failing.
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Component:
    """One entry in the vocabulary: a name, what it is for, and its props."""

    name: str
    summary: str
    props: Mapping[str, Prop] = field(default_factory=dict)
    #: Cross-prop rule, if the component has one (`table` row width, `calendar`
    #: row shape). Runs at the door with the props, so it refuses a spec rather
    #: than a render — the same reason every other rule lives here.
    check: Callable[[Mapping[str, Any]], None] | None = None

    def required(self) -> tuple[str, ...]:
        return tuple(key for key, prop in self.props.items() if prop.required)


@dataclass(frozen=True, slots=True)
class Block:
    """One component, with the values it was given."""

    component: str
    props: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "props": {key: _plain(value) for key, value in self.props.items()},
        }


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
    def parse(
        cls,
        raw: Any,
        *,
        vocabulary: Mapping[str, Component],
        values: Mapping[str, Any] | None = None,
    ) -> "Spec":
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
            _parse_block(entry, index, vocabulary, values)
            for index, entry in enumerate(blocks_raw)
        )
        title = _text(raw.get("title", ""), "title", MAX_TITLE) if raw.get("title") else ""
        mood = _text(raw.get("mood", ""), "mood", MAX_MOOD) if raw.get("mood") else ""
        return cls(blocks=blocks, title=title, mood=mood)

    def bind(self, values: Mapping[str, Any], *, vocabulary: Mapping[str, Component]) -> "Spec":
        """Fill every binding from local values, and validate what comes back.

        A resolved value goes through the *same* door as a literal one, so a
        binding cannot smuggle in a shape the prop would have refused — the
        guarantee is not "the model was validated", it is "what renders was".
        """
        return Spec.parse(self.to_dict(), vocabulary=vocabulary, values=values)

    def has_bindings(self) -> bool:
        return any(_has_binding(prop) for block in self.blocks for prop in block.props.values())


def describe(vocabulary: Mapping[str, Component]) -> str:
    """The vocabulary, written for the model — the only components it may use.

    `?` marks a prop that may be omitted; everything else is required.
    """
    lines = [
        "A dashboard is a list of blocks. Each block is "
        '{"component": "<name>", "props": {…}}.',
        "Values are text, never markup: no tags, no scripts, and the only link is a "
        "relative path.",
        'A value you do not have is written {"bind": "dotted.key"}: the renderer fills '
        "it in locally, and you never see the value itself.",
        "",
        "Components (use these names exactly):",
    ]
    for name in sorted(vocabulary):
        component = vocabulary[name]
        props = ", ".join(
            f"{key}: {prop.kind.value}{_choices(prop)}{'' if prop.required else '?'}"
            for key, prop in component.props.items()
        )
        lines.append(f"- {name}({props}) — {component.summary}")
    return "\n".join(lines)


def _choices(prop: Prop) -> str:
    return f"[{'|'.join(prop.choices)}]" if prop.choices else ""


# -- parsing -----------------------------------------------------------------


def _parse_block(
    entry: Any, index: int, vocabulary: Mapping[str, Component], values: Mapping[str, Any] | None
) -> Block:
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
        props[key] = _coerce(prop, value, f"{where} ({name}).{key}", values)
    if component.check is not None:
        # A cross-prop rule refuses the *spec*, at the door, in the same voice as
        # every other refusal — not a render that half-succeeds.
        try:
            component.check(props)
        except SpecError as exc:
            raise SpecError(f"{where} ({name}): {exc}") from exc
    return Block(component=name, props=props)


def _coerce(prop: Prop, value: Any, where: str, values: Mapping[str, Any] | None) -> Any:
    key = _binding_key(value, where)
    if key is not None:
        if values is None:
            return Binding(key)
        if key not in values:
            known = ", ".join(sorted(values)) or "nothing"
            raise SpecError(f"{where} is bound to {key!r}, which nothing here supplies. Available: {known}")
        return _literal(prop, values[key], where, values)
    return _literal(prop, value, where, values)


def _literal(prop: Prop, value: Any, where: str, values: Mapping[str, Any] | None) -> Any:
    kind = prop.kind
    if kind is Kind.TEXT:
        return _choice(_text(value, where), prop, where)
    if kind is Kind.LINK:
        return _choice(_link(value, where), prop, where)
    if kind is Kind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(f"{where} is a number; it is {_typename(value)}")
        return value
    if kind is Kind.BOOL:
        if not isinstance(value, bool):
            raise SpecError(f"{where} is true or false; it is {_typename(value)}")
        return value
    if kind is Kind.LIST:
        return _items(value, where, values)
    if kind is Kind.ROWS:
        return _rows(value, where, values)
    raise SpecError(f"{where}: unknown kind {kind!r}")  # pragma: no cover — closed enum


def _binding_key(value: Any, where: str) -> str | None:
    """A binding, in either spelling a model reaches for, or nothing.

    `{"bind": "disk.free"}` is the canonical form; `"{{disk.free}}"` is accepted
    when it is the *whole* value, so it can never be confused with text that
    happens to contain braces.
    """
    if isinstance(value, Mapping):
        if "bind" not in value:
            return None
        if set(value) != {"bind"}:
            raise SpecError(
                f"{where}: a binding is {{\"bind\": \"key\"}} and nothing else; "
                f"it also carries {', '.join(map(repr, sorted(set(value) - {'bind'})))}"
            )
        return _key(value["bind"], where)
    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        return _key(value[2:-2], where)
    return None


def _key(raw: Any, where: str) -> str:
    key = str(raw).strip()
    if not _KEY.match(key):
        raise SpecError(
            f"{where} is bound to a dotted lower-case key such as `disk.free`; it is {raw!r}"
        )
    return key


def _choice(text: str, prop: Prop, where: str) -> str:
    if prop.choices and text not in prop.choices:
        raise SpecError(f"{where} is one of: {' | '.join(prop.choices)}; it is {text!r}")
    return text


def _items(value: Any, where: str, values: Mapping[str, Any] | None) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise SpecError(f"{where} is a list; it is {_typename(value)}")
    if len(value) > MAX_ITEMS:
        raise SpecError(f"{where} holds {len(value)} items; the limit is {MAX_ITEMS}")
    return [_scalar(item, f"{where}[{index}]", values) for index, item in enumerate(value)]


def _rows(value: Any, where: str, values: Mapping[str, Any] | None) -> list[list[Any]]:
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
        rows.append([_scalar(cell, f"{where}[{index}]", values) for cell in row])
    return rows


def _scalar(value: Any, where: str, values: Mapping[str, Any] | None = None) -> Any:
    key = _binding_key(value, where)
    if key is not None:
        if values is None:
            return Binding(key)
        if key not in values:
            known = ", ".join(sorted(values)) or "nothing"
            raise SpecError(f"{where} is bound to {key!r}, which nothing here supplies. Available: {known}")
        return _scalar(values[key], where, values)
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


def _plain(value: Any) -> Any:
    """Back to JSON: a binding leaves as the dict it arrived as."""
    if isinstance(value, Binding):
        return {"bind": value.key}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _has_binding(value: Any) -> bool:
    if isinstance(value, Binding):
        return True
    if isinstance(value, list):
        return any(_has_binding(item) for item in value)
    return False


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
