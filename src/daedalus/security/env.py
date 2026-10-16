"""The environment a command the agent runs should see: **no secrets in it**.

Anything the agent executes inherits the process environment. That is a
reasonable place for a credential to be sitting — the daemon's own `.env`, or
the owner's shell when the agent is driven from the CLI. And whatever a command
prints comes straight back into the conversation, where it is forwarded to a
model provider.

So the rule from AGENTS.md — structure travels, values stay local — has to be
applied to the process environment too, not just to request bodies. Scrubbing is
name-based and deliberately blunt: a command that genuinely needs a credential
is a *resource*, and resources are provisioned through the capability manifest,
not scraped out of the ambient environment.
"""

from __future__ import annotations

import os
import re
from typing import Mapping

from ..config import Settings

# Names that carry a value rather than a setting. Erring wide is cheap: the
# cost of scrubbing something harmless is that a command does not see it.
_SECRET_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PRIVATE|_KEY$|APIKEY|AUTH)",
    re.IGNORECASE,
)


def looks_secret(name: str) -> bool:
    return bool(_SECRET_NAME.search(name))


def scrubbed(settings: Settings | None = None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with credentials, and everything from `.env`, removed."""
    env = dict(os.environ if base is None else base)
    known = set(settings.env) if settings is not None else set()
    for name in list(env):
        # `settings.env` is the install's own `.env`, read verbatim; every key in
        # it is something the owner put there for Daedalus, not for arbitrary
        # commands to read back.
        if name in known or looks_secret(name):
            env.pop(name, None)
    return env
