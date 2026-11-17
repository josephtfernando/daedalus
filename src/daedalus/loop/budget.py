"""How full a model's context window already is, before the provider says no.

Overflow recovery exists and works, but it is an *emergency* path: the request
has already been rejected, a round trip has been spent, and the trim is blind —
it does not know how much room it needs, only that it needs some.

Pi knows each model's window and keeps a reserve for the answer, so a request
that would not fit is made smaller *before* it is sent. This is the same idea
with the cheapest honest estimate available: characters over four. A token
count from a real tokenizer would be better, and it would also be a dependency
and a per-provider table; four characters per token is close enough to decide
"is this ten times too big?" and it never needs updating.

The estimate deliberately over-counts slightly (message overhead is added per
message), because the failure we are avoiding is sending too much.
"""

from __future__ import annotations

import json
from typing import Any

#: Rough characters per token for English prose and JSON. Deliberately low:
#: over-counting trims a little early, under-counting overflows.
CHARS_PER_TOKEN = 4
#: Room kept back for the model's own answer. A turn's reply plus tool-call
#: arguments fits comfortably; the point is to leave *some* room rather than
#: filling the window to the brim and asking for a paragraph.
RESERVE_TOKENS = 2048
#: Fixed per-message cost of role markers and separators.
MESSAGE_OVERHEAD_CHARS = 16


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Roughly how many tokens this message list costs the model."""
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(json.dumps(content, default=str))
        for call in message.get("tool_calls") or []:
            chars += len(json.dumps(call, default=str))
        chars += MESSAGE_OVERHEAD_CHARS
    return chars // CHARS_PER_TOKEN + 1


def over_budget(
    messages: list[dict[str, Any]],
    window: int,
    *,
    extra_chars: int = 0,
) -> bool:
    """Would this request leave less than the reserve inside ``window``?

    ``window`` of 0 means unknown, and unknown always fits: trimming on a guess
    is worse than letting the provider answer and recovering if it refuses.
    ``extra_chars`` is the tool schema's JSON, which also rides in every call.
    """
    if window <= 0:
        return False
    budget = max(0, window - RESERVE_TOKENS)
    used = estimate_tokens(messages) + max(0, extra_chars) // CHARS_PER_TOKEN
    return used > budget
