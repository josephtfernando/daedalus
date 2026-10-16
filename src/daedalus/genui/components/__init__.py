"""The visual vocabulary (SPEC §3.6).

Each component is a **name, a typed prop schema, and a renderer**. The schema is
what :mod:`daedalus.genui.spec` validates a proposal against — the door. The
renderer is the last mile, and it escapes every value it writes *again*, so a bug
in a component cannot become an injection just because the door was the only
guard.

Components carry no styling. They emit semantic markup with class names; the mood
token set (:mod:`daedalus.genui.theming`) supplies the CSS. That split is what
lets one spec be rendered in any mood without the model knowing anything about
colour, and it means the only place CSS is written is a place the owner approved.

The vocabulary is deliberately small. SPEC §3.6 calls components *authored
capabilities* that the owner approves before use — so growing this set is a
proposal, not a drift, and nothing here should be added because it looked nice.
"""

from __future__ import annotations

from html import escape
from typing import Any, Callable, Mapping

from ..spec import Binding, Block, Component, Kind, Prop, Spec, SpecError

__all__ = ["RENDERERS", "VOCABULARY", "render", "render_blocks"]

#: Views the calendar component understands, in the order the owner would say them.
CALENDAR_VIEWS = ("day", "week", "month")


def _table_rows(props: Mapping[str, Any]) -> None:
    """A row may be shorter than the headings, but never longer.

    Short is a cell left blank — visible, harmless, and often what a spec means.
    Long is data with nowhere to go: padding would invent a column, truncating
    would drop the owner's value silently, and both are worse than a refusal.
    """
    columns = len(props.get("columns", ()))
    for index, row in enumerate(props.get("rows", ())):
        if len(row) > columns:
            raise SpecError(
                f"row {index + 1} has {len(row)} cells and there are {columns} columns; "
                "a row is never wider than its headings"
            )


def _calendar_shape(props: Mapping[str, Any]) -> None:
    """Each day is `[date, weekday, what is on it]` — three cells, every time."""
    for index, row in enumerate(props.get("days", ())):
        if len(row) != 3:
            raise SpecError(
                f"day {index + 1} has {len(row)} cells; a day is "
                "[date, weekday, what is on it]"
            )


VOCABULARY: dict[str, Component] = {
    "stat-card": Component(
        name="stat-card",
        summary="one value, stated plainly — a count, a size, a total",
        props={
            "label": Prop(Kind.TEXT, required=True, help="what the value is"),
            "value": Prop(Kind.TEXT, required=True, help="the value itself"),
            "note": Prop(Kind.TEXT, help="a qualifier, a comparison, a trend"),
        },
    ),
    "list": Component(
        name="list",
        summary="an unordered set of short lines",
        props={
            "items": Prop(Kind.LIST, required=True, help="the lines, in order"),
            "empty": Prop(Kind.TEXT, help="what to say when there is nothing"),
        },
    ),
    "timeline": Component(
        name="timeline",
        summary="things in time order — a day's schedule, a run of events",
        props={
            "entries": Prop(Kind.ROWS, required=True, help="each entry: [when, what]"),
            "empty": Prop(Kind.TEXT, help="what to say when nothing is coming"),
        },
    ),
    "table": Component(
        name="table",
        summary="rows under headings — the honest way to show several attributes",
        props={
            "columns": Prop(Kind.LIST, required=True, help="the headings, in order"),
            "rows": Prop(Kind.ROWS, required=True, help="a row per item, matching the headings"),
        },
        check=_table_rows,
    ),
    "prose": Component(
        name="prose",
        summary="a short paragraph, for the thing that is not a number or a list",
        props={
            "text": Prop(Kind.TEXT, required=True, help="the sentence or two to say"),
            "heading": Prop(Kind.TEXT, help="an optional heading above it"),
        },
    ),
    "calendar": Component(
        name="calendar",
        summary="a day, week or month laid out as a grid",
        props={
            "view": Prop(
                Kind.TEXT,
                required=True,
                choices=CALENDAR_VIEWS,
                help="how much time to show",
            ),
            "days": Prop(
                Kind.ROWS,
                required=True,
                help="a day per row: [date, weekday, what is on it]",
            ),
            "today": Prop(Kind.TEXT, help="the date to mark as today, YYYY-MM-DD"),
        },
        check=_calendar_shape,
    ),
}


# -- rendering --------------------------------------------------------------


def _cell(value: Any) -> str:
    """One value, escaped. Booleans read as words rather than as Python."""
    if isinstance(value, Binding):
        # Reaching a renderer means the binding step was skipped. That is a bug,
        # and printing `Binding(key=...)` on the owner's dashboard would be a
        # strange way to report it.
        raise SpecError(f"{value.key!r} was never bound to a value")
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape(str(value))


def _text_of(value: Any) -> str:
    return _cell(value)


def _stat_card(props: Mapping[str, Any]) -> str:
    note = (
        f'\n  <p class="stat-card__note">{_text_of(props["note"])}</p>'
        if props.get("note")
        else ""
    )
    return (
        '<section class="stat-card">'
        f'\n  <p class="stat-card__label">{_text_of(props["label"])}</p>'
        f'\n  <p class="stat-card__value">{_text_of(props["value"])}</p>{note}'
        "\n</section>"
    )


def _list(props: Mapping[str, Any]) -> str:
    items = props["items"]
    if not items:
        return f'<p class="list__empty">{_text_of(props.get("empty") or "Nothing to list.")}</p>'
    lines = "\n".join(f'  <li class="list__item">{_text_of(item)}</li>' for item in items)
    return f'<ul class="list">\n{lines}\n</ul>'


def _timeline(props: Mapping[str, Any]) -> str:
    entries = props["entries"]
    if not entries:
        return f'<p class="timeline__empty">{_text_of(props.get("empty") or "Nothing scheduled.")}</p>'
    lines = []
    for entry in entries:
        when = _text_of(entry[0]) if entry else ""
        what = " ".join(_text_of(cell) for cell in entry[1:])
        lines.append(
            '  <li class="timeline__entry">'
            f'<span class="timeline__when">{when}</span>'
            f'<span class="timeline__what">{what}</span></li>'
        )
    return '<ol class="timeline">\n' + "\n".join(lines) + "\n</ol>"


def _table(props: Mapping[str, Any]) -> str:
    columns = props["columns"]
    head = "".join(f'<th scope="col">{_text_of(column)}</th>' for column in columns)
    body = []
    for row in props["rows"]:
        cells = [_text_of(cell) for cell in row]
        cells += [""] * (len(columns) - len(cells))  # a short row is a blank cell
        body.append("  <tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return (
        '<table class="table">'
        f"\n  <thead><tr>{head}</tr></thead>"
        "\n  <tbody>\n" + "\n".join(body) + "\n  </tbody>\n</table>"
    )


def _prose(props: Mapping[str, Any]) -> str:
    heading = (
        f'\n  <h2 class="prose__heading">{_text_of(props["heading"])}</h2>'
        if props.get("heading")
        else ""
    )
    return f'<section class="prose">{heading}\n  <p>{_text_of(props["text"])}</p>\n</section>'


def _calendar(props: Mapping[str, Any]) -> str:
    today = str(props.get("today", ""))
    days = []
    for row in props["days"]:
        date, weekday, what = row
        mark = ' class="calendar__day calendar__day--today"' if today and date == today else ' class="calendar__day"'
        days.append(
            f"  <li{mark}>"
            f'<span class="calendar__date">{_text_of(date)}</span>'
            f'<span class="calendar__weekday">{_text_of(weekday)}</span>'
            f'<span class="calendar__what">{_text_of(what)}</span></li>'
        )
    return (
        f'<div class="calendar calendar--{escape(props["view"])}">\n  <ol class="calendar__days">\n'
        + "\n".join(days)
        + "\n  </ol>\n</div>"
    )


RENDERERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "stat-card": _stat_card,
    "list": _list,
    "timeline": _timeline,
    "table": _table,
    "prose": _prose,
    "calendar": _calendar,
}


def render(block: Block) -> str:
    """Render one *validated* block to an HTML fragment.

    Every value is escaped here as well as at the door: this is the second of the
    two defences, and the one that holds when a component is added carelessly.
    """
    renderer = RENDERERS.get(block.component)
    if renderer is None:  # pragma: no cover — the door refuses unknown components
        raise SpecError(f"{block.component!r} has no renderer")
    return renderer(block.props)


def render_blocks(spec: Spec) -> str:
    return "\n".join(render(block) for block in spec.blocks)
