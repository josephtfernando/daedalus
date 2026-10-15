"""Sink policy (ARCHITECTURE §10).

The egress sink checks the provenance of the value that *authorizes* an action,
not merely the payload. Untrusted text can shape content; it can never set a
recipient, a URL, a command, or a scope.
"""

from __future__ import annotations

from collections.abc import Iterable

from .provenance import Labeled


def may_authorize(values: Iterable[Labeled]) -> tuple[bool, str]:
    """True only if every authorizing value is trusted or local-derived."""
    for v in values:
        if not v.may_authorize():
            ref = f" ({v.source_ref})" if v.source_ref else ""
            return False, f"untrusted value may not authorize an action{ref}"
    return True, "ok"


def may_egress(value: Labeled) -> tuple[bool, str]:
    """True only if the value's data class permits leaving the machine."""
    if not value.may_egress():
        return False, "local_only value may not leave the machine"
    return True, "ok"
