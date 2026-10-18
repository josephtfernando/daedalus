"""The calendar dashboard: a day, a week, a month (SPEC §5.5).

*"The internal calendar has a full dashboard view (`serve`) — day / week / month
— used the way gcal is used."* This module builds that view; the server serves it.

Three rules shape it.

**Occurrences, not definitions.** Every other read in the calendar answers with
what *happens* in a window: a weekly standup is Tuesday's standup, and an instance
the owner moved is where they moved it. A dashboard built from definitions would
show a repeating event once, on the day its rule began, and look broken for the
rest of the year. So each day is read with the store's own day addressing, which
is also the addressing the model uses to say *"the things on Sunday"*.

**The spec carries keys; the values are read here.** The page is composed exactly
the way a model-composed page is (T3, T6): the spec says *what to draw* and the
store's data is bound in locally at render time. That is not ceremony for a page
no model will ever see — it is the same seam, kept in the same shape, so nothing
here has to change if the dashboard later grows a generative header.

**`local_only` events are shown.** This page is the owner's own surface on the
owner's own loopback with no model in the loop, and §6's withholding rule is about
*egress*. Hiding the owner's own appointment from the owner's own calendar would
be the rule turned inside out.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..calendar.event import Event, parse_iso
from ..calendar.store import CalendarStore
from .components import VOCABULARY
from .renderer import CSP, FORM_CSP, render_page
from .spec import Spec
from .theming import Tokens, infer

__all__ = ["VIEWS", "Page", "calendar_page", "draft_strip", "write_page"]

#: The three views, in the order the owner would name them.
VIEWS = ("day", "week", "month")

#: A page is not a report: past a few entries a cell is illegible, and the day
#: view has the full list underneath it anyway.
CELL_LIMIT = 3

#: A month is at most 31 days, and each day's read is bounded — but a day can be
#: busy, so the cap on a day's occurrences is generous rather than tight.
DAY_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Page:
    """A rendered-able page: what to draw, and the local values behind it."""

    view: str
    anchor: date
    spec: Spec
    values: dict[str, Any]
    mood: str = ""
    #: The control strip, as markup. Built here, by code, and deliberately
    #: *outside* the spec: the model composes what a page says, never what it
    #: can do (SPEC §3.6 — visual → dashboard, action → Discord).
    controls: str = ""
    #: The policy this page claims for itself. A page with controls needs
    #: `form-action 'self'`; a read-only one keeps the strict default.
    csp: str = CSP
    #: The owner's **adopted** theme values (T9), laid over the mood. Absent
    #: means the mood alone.
    overrides: Mapping[str, str] | None = None
    #: A token set given outright — a **draft preview**, the page as a pending
    #: tune would render it. Nothing is resolved when this is set.
    tokens: Tokens | None = None

    @property
    def name(self) -> str:
        return f"{self.view}-{self.anchor.isoformat()}.html"

    def html(self) -> str:
        return render_page(
            self.spec,
            values=self.values,
            mood=self.mood,
            appendix=self.controls,
            extra_css=_CONTROL_CSS if self.controls else "",
            csp=self.csp,
            overrides=self.overrides,
            tokens=self.tokens,
        )


def calendar_page(
    calendar: CalendarStore,
    *,
    view: str = "day",
    anchor: str | date | None = None,
    now: datetime | None = None,
    with_controls: bool = False,
    overrides: Mapping[str, str] | None = None,
    tokens: Tokens | None = None,
    roots: list[tuple[str, str]] | None = None,
) -> Page:
    """One view of the calendar, as a page ready to render.

    `anchor` is the day the view is *about* — a week or a month is the one
    containing it. `now` decides which day is marked as today, and is passed in
    rather than read, so a test can pin the clock. `with_controls` adds the
    control strip, which only an authenticated view is served with. `overrides`
    are the owner's adopted theme values; `tokens` given outright renders a
    **draft preview** (T9) — the page as a pending tune would look. `roots` is
    the declared servable root list, for the editor section of the strip.

    The mood is `infer`red from the same local signals the page is built from —
    the hour, and how crowded today is — unless the caller names one. That is
    the ticket's "dynamic for what I asked for in the moment", done the honest
    way: no model in the loop, nothing remote, and the owner's adopted
    overrides still sit on top of whatever is inferred.
    """
    if view not in VIEWS:
        raise ValueError(f"view is one of: {' | '.join(VIEWS)}; it is {view!r}")
    moment = now or datetime.now()
    today = moment.date()
    day = _as_date(anchor) or today
    days = _window_days(view, day)
    on_day = {when: calendar.occurrences_on(when.isoformat(), limit=DAY_LIMIT) for when in days}

    if view == "day":
        blocks: list[dict[str, Any]] = [
            _calendar_block("day", {"bind": "view.cells"}, bind_today=True),
            {
                "component": "timeline",
                "props": {"entries": {"bind": "view.entries"}, "empty": "Nothing on."},
            },
        ]
        values = {
            "view.cells": [_day_cell(days[0], on_day[days[0]], count=True)],
            "view.entries": [[_when(event), _what(event)] for event in on_day[days[0]]],
        }
        count = len(on_day[days[0]])
        counts = f"{count} thing{'' if count == 1 else 's'}" if count else "Nothing on"
    else:
        blocks = [
            _calendar_block(view, {"bind": "view.cells"}, bind_today=True),
            {
                "component": "stat-card",
                "props": {
                    "label": "This week" if view == "week" else "This month",
                    "value": {"bind": "view.count"},
                },
            },
        ]
        values = {"view.cells": [_day_cell(when, on_day[when]) for when in days]}
        count = sum(len(events) for events in on_day.values())
        counts = f"{count} thing{'' if count == 1 else 's'}" if count else "Nothing on"

    values["page.today"] = today.isoformat()
    values["view.count"] = counts
    # Parsed *without* the values, so the spec that exists holds keys and never
    # the owner's data: the page is bound at render time, exactly as a
    # model-composed page is. `page.spec.to_dict()` is therefore safe to show,
    # log or hand back.
    spec = Spec.parse({"title": _long_date(day), "blocks": blocks}, vocabulary=VOCABULARY)
    chosen_mood = infer(hour=moment.hour, busy_blocks=len(on_day[day]))
    controls = (
        _control_strip(view, day, on_day[days[0]], roots=roots) if with_controls else ""
    )
    return Page(
        view=view,
        anchor=day,
        spec=spec,
        values=values,
        mood=chosen_mood,
        controls=controls,
        csp=FORM_CSP if controls else CSP,
        overrides=overrides,
        tokens=tokens,
    )


def write_page(page: Page, home: Path, *, front: bool = False) -> Path:
    """Write the page into ``$DAEDALUS_HOME/ui/``, where T1 serves it from.

    `front` additionally makes it the server's entry page — which is what the
    owner opening the laptop wants, and what makes ``/`` *the dashboard* rather
    than a directory of files.
    """
    ui = Path(home) / "ui"
    ui.mkdir(parents=True, exist_ok=True)
    body = page.html()
    written = ui / page.name
    written.write_text(body, encoding="utf-8")
    if front:
        (ui / "index.html").write_text(body, encoding="utf-8")
    return written


# -- the shape of each view --------------------------------------------------


def _calendar_block(view: str, days: Any, *, bind_today: bool) -> dict[str, Any]:
    props: dict[str, Any] = {"view": view, "days": days}
    if bind_today:
        props["today"] = {"bind": "page.today"}
    return {"component": "calendar", "props": props}


def _window_days(view: str, anchor: date) -> list[date]:
    """The days a view draws.

    A week starts on **Monday** and a month is drawn as whole weeks, because a
    month grid whose first row is four cells wide is not a calendar — it is a
    list. The days that spill in from the neighbouring months are drawn as dates
    with nothing beside them, which is the honest thing to show: the day exists,
    and it is not this month's.
    """
    if view == "day":
        return [anchor]
    if view == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        return [monday + timedelta(days=offset) for offset in range(7)]
    first = anchor.replace(day=1)
    start = first - timedelta(days=first.weekday())
    last = (first + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    end = last + timedelta(days=6 - last.weekday())
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _day_cell(when: date, events: list[Event], *, count: bool = False) -> list[str]:
    """One grid cell: the date, its weekday, and what is on it."""
    if count:
        # The day view has the schedule *under* the grid, so the cell is a
        # summary of it rather than a second copy.
        text = f"{len(events)} thing{'' if len(events) == 1 else 's'}" if events else "Nothing on"
    else:
        shown = [_label(event) for event in events[:CELL_LIMIT]]
        if len(events) > CELL_LIMIT:
            shown.append(f"+{len(events) - CELL_LIMIT} more")
        text = " · ".join(shown)
    return [when.isoformat(), when.strftime("%a"), text]


def _label(event: Event) -> str:
    clock = _clock(event)
    return f"{clock} {event.summary}".strip()


def _when(event: Event) -> str:
    """The timeline's left column: a clock, or the fact that there is no clock."""
    return "all day" if event.all_day else _clock(event)


def _clock(event: Event) -> str:
    return parse_iso(event.start).strftime("%H:%M")


def _what(event: Event) -> str:
    return f"{event.summary} @ {event.location}" if event.location else event.summary


def _as_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"a date is YYYY-MM-DD; it is {value!r}") from None


def _long_date(when: date) -> str:
    """`Friday 16 October` — a heading, without a locale-dependent ordinal."""
    return f"{when.strftime('%A')} {when.day} {when.strftime('%B')}"


# -- the control strip (SPEC §3.6 — visual → dashboard, action → Discord) ----
#
# Built here, by code, and never by the model. The strip is markup *outside* the
# spec, so a page the model authored carries no actions at all: its forms do not
# exist to be composed, only to be proposed — and every POST lands in
# `genui/actions.py`, which resolves it to a tool call and puts it through the
# same approval middleware the loop's own calls get.

_CONTROL_CSS = (
    # Composed from the token set plus its derived scales (T35), so the
    # controls stop being a fixed dark island inside whatever mood the page is
    # in — they are made of the same materials, and focus is visible in all of
    # them.
    ".controls{margin-top:var(--d-space-5);border-top:1px solid var(--d-border);"
    "padding-top:var(--d-space-3);display:grid;gap:var(--d-space-2)}"
    ".controls h2{font-size:var(--d-type-xs);letter-spacing:.08em;text-transform:uppercase;"
    "color:var(--d-muted);margin:0}"
    ".controls form{display:flex;flex-wrap:wrap;gap:var(--d-space-2);align-items:center;"
    "margin:0}"
    ".controls input,.controls select{background:var(--d-bg);color:var(--d-text);"
    "border:1px solid var(--d-border);border-radius:var(--d-radius-sm);"
    "padding:var(--d-space-2);font:inherit}"
    ".controls button{background:var(--d-accent);color:var(--d-accent_text);border:0;"
    "border-radius:var(--d-radius-sm);padding:var(--d-space-2) var(--d-space-3);"
    "font:inherit;cursor:pointer;transition:filter var(--d-motion) ease,"
    "box-shadow var(--d-motion) ease}"
    ".controls button:hover{filter:brightness(1.12)}"
    ".controls button:focus-visible,.controls input:focus-visible,"
    ".controls select:focus-visible{outline:none;box-shadow:var(--d-ring)}"
    ".controls .danger{background:var(--d-alert)}"
    ".controls label{font-size:var(--d-type-xs);color:var(--d-muted)}"
    ".controls .roots__row{display:flex;gap:var(--d-space-2);align-items:center;"
    "margin:0}"
    ".controls code{font-family:var(--d-font-mono);font-size:var(--d-type-xs)}"
    ".controls .controls--note{font-size:var(--d-type-xs);color:var(--d-muted);margin:0}"
)


def _control_strip(
    view: str,
    day: date,
    events: list[Event],
    roots: list[str] | None = None,
) -> str:
    """The forms a page carries, as one strip under the view.

    Every form POSTs to `/action` with a closed control name and flat fields.
    The day is pre-filled with the day the view is about, so "add something"
    needs two fields and nothing else. Only the day view offers a picker for
    move and remove — its list *is* that day's events; a week or a month is a
    grid of summaries, and a picker over a month of instances is a trap. The
    roots editor (T35) is on every view that carries an authenticated session:
    reach is global, not per-view.
    """
    strip: list[str] = []
    day_value = day.isoformat()
    add = (
        '<form method="post" action="/action">'
        '<input type="hidden" name="control" value="calendar.create">'
        f'<input type="datetime-local" name="start" value="{day_value}T09:00" '
        'aria-label="when">'
        '<input name="summary" placeholder="what it is" required aria-label="what it is">'
        '<button type="submit">Add</button>'
        "</form>"
    )
    if view != "day":
        strip += ["<h2>Add an event</h2>", add, "<p>Open a day to move or remove one of its events.</p>"]
    else:
        move = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="control" value="calendar.update">'
            f"{_uid_picker(events)}"
            '<input type="datetime-local" name="start" aria-label="new time" required>'
            '<button type="submit">Move</button>'
            "</form>"
        )
        remove = (
            '<form method="post" action="/action">'
            '<input type="hidden" name="control" value="calendar.delete">'
            f"{_uid_picker(events)}"
            '<button type="submit" class="danger">Remove</button>'
            "</form>"
        )
        strip += [
            "<h2>Add to this day</h2>",
            add,
            "<h2>Move an event</h2>",
            move,
            "<h2>Remove an event</h2>",
            remove,
        ]
    strip.append(_roots_section(roots or []))
    body = "\n".join(strip)
    return f'<section class="controls">\n{body}\n</section>'


def _roots_section(roots: list[str]) -> str:
    """The servable-root editor (T35, carried from T34).

    The list is shown as the owner typed it, one row per folder with its own
    remove form, and an add row at the end. Both verbs are ASKs at the tool,
    so a submit returns "waiting" and the buttons land on Discord — the strip
    says so, rather than implying a click is all it takes.
    """
    rows = []
    for root in roots:
        rows.append(
            '<div class="roots__row">'
            f"<code>{html.escape(root)}</code>"
            '<form method="post" action="/action">'
            '<input type="hidden" name="control" value="settings.roots-remove">'
            f'<input type="hidden" name="path" value="{html.escape(root)}">'
            '<button type="submit" class="danger">Remove</button>'
            "</form>"
            "</div>"
        )
    listing = (
        "\n".join(rows)
        or '<p class="controls--note">No folders beyond the always-allowed ones.</p>'
    )
    return (
        "<h2>Servable roots</h2>\n"
        f"{listing}\n"
        '<form method="post" action="/action">'
        '<input type="hidden" name="control" value="settings.roots-add">'
        '<input name="path" placeholder="absolute folder to allow" required aria-label="folder">'
        '<button type="submit">Add root</button>'
        "</form>\n"
        '<p class="controls--note">Adding or removing one asks on Discord before it stands.</p>'
    )


def _uid_picker(events: list[Event]) -> str:
    """A `<select>` of one day's occurrences, named as the owner sees them.

    The value is the *address* the agenda prints — a series by its uid, one
    instance as ``uid@start`` — which the control surface splits into the two
    arguments the tool takes.
    """
    if not events:
        return '<input type="hidden" name="uid" value="">'
    options = "\n".join(
        f'<option value="{html.escape(event.address)}">'
        f"{html.escape(_picker_label(event))}</option>"
        for event in events
    )
    return f'<select name="uid" aria-label="event">\n{options}\n</select>'


def _picker_label(event: Event) -> str:
    return f"{_when(event)} — {event.summary}"


def draft_strip(id: str) -> str:
    """The approve/discard forms a **draft preview** carries (SPEC §3.6).

    Approval is a control on the page being looked at — *seeing is the most
    accurate way to say yes* — so the preview is the real dashboard, rendered
    with the pending set, and the two choices are right under it. Built by code,
    outside the spec, exactly like the control strip.
    """
    return (
        '<section class="controls">\n'
        "<h2>This tune is pending</h2>\n"
        '<form method="post" action="/approve">'
        f'<input type="hidden" name="id" value="{_escape(id)}">'
        '<input type="hidden" name="decision" value="approve">'
        '<button type="submit">Adopt this theme</button>'
        "</form>\n"
        '<form method="post" action="/approve">'
        f'<input type="hidden" name="id" value="{_escape(id)}">'
        '<input type="hidden" name="decision" value="deny">'
        '<button type="submit" class="danger">Discard</button>'
        "</form>\n"
        "</section>"
    )


def _escape(text: str) -> str:
    return html.escape(str(text), quote=True)
