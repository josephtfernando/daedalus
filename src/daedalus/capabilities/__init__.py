"""The capability substrate: author, verify, register, run (SPEC §3.5)."""

from .authoring import materialize
from .manifest import Caps, Manifest, Scopes
from .registry import CapabilityRegistry
from .runner import CapabilityRunner, RunResult
from .verify import VerifyReport, verify_capability

__all__ = [
    "Caps",
    "Manifest",
    "Scopes",
    "CapabilityRegistry",
    "CapabilityRunner",
    "RunResult",
    "VerifyReport",
    "verify_capability",
    "materialize",
]
