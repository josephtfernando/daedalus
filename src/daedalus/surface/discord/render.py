"""Make a model's Markdown survive Discord.

Discord renders bold, italics, lists and code blocks — but **not** tables. A
GFM table therefore reaches the owner as a wall of pipes, which is worse than
no table: the information is there and unreadable. The model cannot be trusted
to remember this (and asking it not to write tables costs a turn when it
forgets), so the surface converts them on the way out.

The conversion is deliberately narrow:

- only a real table — a header row, a separator row of ``---`` cells, and at
  least one body row of the same width — is touched;
- a table already inside a fenced code block is left alone (no nested fences);
- anything else passes through byte for byte, including a half-written table
  in a streamed preview.

This runs on the final text, never on individual streamed deltas: a partial
table is not a table, and the preview is a preview. What the owner keeps is
the rendered answer.
"""

from __future__ import annotations

import re

_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_PIPE = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> list[str]:
    """Split a row on unescaped pipes, dropping the decorative outer ones."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in _PIPE.split(stripped)]


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return len(cells) >= 2 and all(_SEPARATOR_CELL.match(cell) for cell in cells)


def _as_block(header: list[str], rows: list[list[str]]) -> list[str]:
    width = len(header)
    sizes = [max(len(row[col]) for row in [header, *rows]) for col in range(width)]
    lines = [
        "| " + " | ".join(cell.ljust(sizes[col]) for col, cell in enumerate(header)) + " |",
        "|" + "|".join("-" * (size + 2) for size in sizes) + "|",
    ]
    lines.extend(
        "| " + " | ".join(cell.ljust(sizes[col]) for col, cell in enumerate(row)) + " |"
        for row in rows
    )
    fence = "````" if any("```" in line for line in lines) else "```"
    return [fence, *lines, fence]


def discord_markdown(text: str) -> str:
    """Wrap GFM tables in a code block; leave every other line untouched.

    Idempotent by construction: the second pass sees a fenced block and skips
    it. That matters because the same text can reach a preview edit and a final
    send, and neither should acquire a second layer of fences.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence and i + 1 < len(lines) and "|" in line and _is_separator(lines[i + 1]):
            header = _cells(line)
            if len(header) >= 2 and len(header) == len(_cells(lines[i + 1])):
                j = i + 2
                rows: list[list[str]] = []
                while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                    row = _cells(lines[j])
                    if len(row) != len(header):
                        break
                    rows.append(row)
                    j += 1
                if rows:
                    out.extend(_as_block(header, rows))
                    i = j
                    continue
        out.append(line)
        i += 1
    return "\n".join(out)
