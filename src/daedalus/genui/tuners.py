"""Tuners: a tune is a proposal, and only approval is a theme (SPEC §3.6).

*"Tuned values form a **pending** set; **approve** adopts them into the
owner-approved store."* The point of the two-step is not ceremony, it is that
*seeing* is how a theme is judged: a tune renders as a **preview of the real
page**, and the live theme does not move until the owner has looked at it and
said yes.

Four rules shape it:

1. **The live theme is what was approved.** A tune writes a draft under
   ``genui/drafts/`` and touches nothing else; only :meth:`Tuners.approve` writes
   ``genui/tokens/approved.toml``, which is the only file the renderer reads. A
   restart finds the pending set exactly where it was left, because it is files.
2. **Every value passes the door twice.** Once when it is tuned and once when it
   is adopted — the second time because ``approved.toml`` is a file the owner
   edits by hand, and a hand-edited value is entitled to the same refusal as a
   proposed one.
3. **A draft is a file, so "never mind" is deleting it.** Discarding is not a
   command the owner has to remember; it is removing the file they can see.
4. **Tokens here, knobs later.** §3.6 also names system knobs, and the shape
   (a partial set, pending, previewed, adopted) is built to carry them — but the
   theme is what has a renderer to preview with, so it is what ships.
"""

from __future__ import annotations

import logging
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..memory.schema import ulid
from .theming import TOKEN_NAMES, Tokens, apply, check_token, resolve

__all__ = ["Draft", "TuneError", "Tuners"]

log = logging.getLogger("daedalus.genui.tuners")

#: Where the owner's adopted values live, and where a tune waits.
APPROVED_FILE = "tokens/approved.toml"
DRAFTS_DIR = "drafts"


class TuneError(ValueError):
    """A tune that cannot be believed — named by what was wrong with it."""


@dataclass(frozen=True, slots=True)
class Draft:
    """One pending tune: a partial token set, and nothing else."""

    id: str
    values: dict[str, str]
    note: str = ""
    created_at: float = 0.0

    def describes(self) -> str:
        """What this draft would change, in the owner's words."""
        return ", ".join(f"{name} → {value}" for name, value in sorted(self.values.items()))


class Tuners:
    """The pending → approved path for the theme (SPEC §3.6, ARCHITECTURE §9.5)."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.approved_file = self.home / "genui" / APPROVED_FILE
        self.drafts_dir = self.home / "genui" / DRAFTS_DIR

    # -- what renders today --------------------------------------------------

    def approved(self) -> dict[str, str]:
        """The owner's adopted values. A file that cannot be believed is refused."""
        if not self.approved_file.exists():
            return {}
        try:
            raw = tomllib.loads(self.approved_file.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise TuneError(f"{self.approved_file}: {exc}") from exc
        values = raw.get("tokens", {})
        if not isinstance(values, Mapping):
            raise TuneError(f"{self.approved_file}: `tokens` is a table of token = value")
        unknown = sorted(set(values) - set(TOKEN_NAMES))
        if unknown:
            raise TuneError(
                f"{self.approved_file}: unknown token{'' if len(unknown) == 1 else 's'} "
                f"{', '.join(map(repr, unknown))} — a theme has: {', '.join(TOKEN_NAMES)}"
            )
        return {name: str(value) for name, value in values.items()}

    def theme(self, *, inferred: str = "", override: str = "") -> Tokens:
        """The theme a page renders with: a mood, plus what the owner approved."""
        try:
            return resolve(inferred=inferred, override=override, overrides=self.approved())
        except ValueError as exc:
            # A hand-edited approved file is the owner's own error to see: name
            # the file, and refuse in the same voice as a refused tune.
            raise TuneError(f"{self.approved_file}: {exc}") from exc

    # -- the pending set -----------------------------------------------------

    def tune(self, values: Mapping[str, Any], *, note: str = "") -> Draft:
        """Propose a partial token set. Validated now; it renders only as a preview."""
        if not isinstance(values, Mapping) or not values:
            raise TuneError("a tune is a non-empty object of token = value")
        unknown = sorted(set(values) - set(TOKEN_NAMES))
        if unknown:
            raise TuneError(
                f"unknown token{'' if len(unknown) == 1 else 's'} {', '.join(map(repr, unknown))} — "
                f"a theme has: {', '.join(TOKEN_NAMES)}"
            )
        cleaned = {name: str(value).strip() for name, value in values.items()}
        for name, value in cleaned.items():
            try:
                check_token(name, value)
            except ValueError as exc:
                raise TuneError(str(exc)) from exc
        if not any(value != "" for value in cleaned.values()):
            raise TuneError("a tune changes something; every value given is empty")
        draft = Draft(
            id=ulid(),
            values={name: value for name, value in cleaned.items() if value},
            note=str(note or "").strip(),
            created_at=time.time(),
        )
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        _write_draft(self.drafts_dir / f"{draft.id}.toml", draft)
        log.info("tune %s pending: %s", draft.id, draft.describes())
        return draft

    def pending(self) -> list[Draft]:
        """The drafts waiting, oldest first. A file that cannot be read is refused."""
        if not self.drafts_dir.is_dir():
            return []
        drafts = [
            _read_draft(path)
            for path in sorted(self.drafts_dir.glob("*.toml"))
        ]
        return sorted(drafts, key=lambda d: (d.created_at, d.id))

    def draft(self, id: str) -> Draft | None:
        """One pending draft, or nothing — an unknown id is a 404, not an error."""
        path = self.drafts_dir / f"{_safe_id(id)}.toml"
        return _read_draft(path) if path.is_file() else None

    def preview(self, id: str) -> Tokens:
        """What the page would look like if this draft were adopted."""
        draft = self.draft(id)
        if draft is None:
            raise TuneError(f"no pending tune {id!r}")
        base = self.theme()
        return apply(base, draft.values)

    # -- adopting ------------------------------------------------------------

    def approve(self, id: str) -> Tokens:
        """Adopt one draft: it becomes the live theme, and stops being pending."""
        draft = self.draft(id)
        if draft is None:
            raise TuneError(f"no pending tune {id!r}")
        merged = {**self.approved(), **draft.values}
        # Validated *before* it is written: a hand-edit gets this door too, so
        # the file on disk is never a set the renderer would choke on.
        apply(self.theme(), draft.values)
        self.approved_file.parent.mkdir(parents=True, exist_ok=True)
        self.approved_file.write_text(_approved_text(merged), encoding="utf-8")
        self.discard(id)
        log.info("tune %s adopted: %s", draft.id, draft.describes())
        return self.theme()

    def deny(self, id: str) -> bool:
        """Discard one draft. Removing the file is the same act, on purpose."""
        return self.discard(id)

    def discard(self, id: str) -> bool:
        path = self.drafts_dir / f"{_safe_id(id)}.toml"
        if not path.is_file():
            return False
        path.unlink()
        return True


# -- the files ----------------------------------------------------------------


def _safe_id(id: str) -> str:
    """A draft id is a file name, so it is matched before it is joined."""
    text = str(id or "").strip()
    if not text or not all(part.isalnum() or part == "-" for part in text) or len(text) > 40:
        raise TuneError(f"{id!r} is not a draft id")
    return text


def _write_draft(path: Path, draft: Draft) -> None:
    lines = [
        "# a pending tune — adopt it with `approve`, or delete this file to discard it",
        f"created = {draft.created_at}",
    ]
    if draft.note:
        lines.append(f"note = {_toml_string(draft.note)}")
    lines.append("")
    lines.append("[tokens]")
    lines.extend(f"{name} = {_toml_string(value)}" for name, value in draft.values.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_draft(path: Path) -> Draft:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise TuneError(f"{path}: {exc}") from exc
    values = raw.get("tokens", {})
    if not isinstance(values, Mapping) or not values:
        raise TuneError(f"{path}: a draft holds a non-empty [tokens] table")
    unknown = sorted(set(values) - set(TOKEN_NAMES))
    if unknown:
        raise TuneError(
            f"{path}: unknown token{'' if len(unknown) == 1 else 's'} "
            f"{', '.join(map(repr, unknown))}"
        )
    return Draft(
        id=path.stem,
        values={name: str(value) for name, value in values.items()},
        note=str(raw.get("note", "")),
        created_at=float(raw.get("created", 0.0)),
    )


def _approved_text(values: Mapping[str, str]) -> str:
    lines = [
        "# the theme values the owner adopted, laid over whichever mood is chosen.",
        "# Edit by hand if you like: every value is checked when it is read.",
        "",
        "[tokens]",
    ]
    lines.extend(f"{name} = {_toml_string(values[name])}" for name in sorted(values))
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    import json

    return json.dumps(str(value), ensure_ascii=False)
