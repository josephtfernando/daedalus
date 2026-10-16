"""Publishing the dashboard to the tailnet — and never past it (SPEC §3.5, T2).

*"Loopback-bound, no public Funnel."* The dashboard's two gates are an inner one
(the loopback bind, with a token on every write) and an outer one (the tailnet's
own authentication). Publishing is the act of handing the inner gate to the outer
one: `tailscale serve` puts the already-private loopback port behind the tailnet,
reachable only by the owner's own devices.

**Funnel is the gate removed.** `tailscale funnel` exposes a port to the whole
internet behind nothing but a TLS certificate. This module never constructs that
command — not as a flag, not as a fallback — and it refuses to publish at all
when the node already has Funnel on, because a public exposure and a private
dashboard on the same node is exactly the shape of "the outer gate is gone and
nobody noticed".

**The command is printed before it is run.** An owner who is about to publish
something is entitled to see the exact line first; a command that only appears in
the shell history after the fact was never really shown.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess  # noqa: S404 — the command is built here, from literals
from dataclasses import dataclass, field
from typing import Callable, Sequence

__all__ = ["PublishError", "Published", "publish", "unpublish"]

log = logging.getLogger("daedalus.genui.publish")

#: The one binary this module will ask for, and the only subcommands it runs.
BIN = "tailscale"


class PublishError(RuntimeError):
    """Publishing is refused, with the reason and usually the way out."""


@dataclass(frozen=True, slots=True)
class Published:
    """What was published, and how — for the log and for taking it back."""

    url: str
    command: Sequence[str]
    #: The full transcript of what was run, in order, for the owner to read.
    ran: Sequence[Sequence[str]] = field(default_factory=list)


def _default_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        # Never let a CLI read the owner's terminal: an unauthenticated
        # `tailscale` that stops to ask would hang the publish silently.
        stdin=subprocess.DEVNULL,
    )


def publish(
    port: int,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _default_run,
) -> Published:
    """Put the loopback dashboard behind the tailnet, and never in front of it.

    The command is *printed* here — the caller's log carries it — before it is
    run, and the returned :class:`Published` carries the tailnet URL when the
    CLI was willing to say one.
    """
    if which(BIN) is None:
        raise PublishError(
            f"{BIN} is not installed, so there is no tailnet to publish to — the "
            "dashboard stays on the loopback. Install Tailscale and log in, then "
            "run `daedalus serve --publish` again."
        )

    funnel = run([BIN, "serve", "status", "--json"])
    if funnel.returncode == 0:
        enabled = _funnel_enabled(funnel.stdout)
        if enabled:
            raise PublishError(
                "tailscale Funnel is on, which would make the dashboard public — "
                "this publish is refused. Take Funnel down "
                f"(`{BIN} funnel --https=443 off`) and publish again."
            )
    else:
        log.warning(
            "could not read the tailscale serve config (%s); Funnel state unverified",
            (funnel.stderr or funnel.stdout).strip()[:120],
        )

    command = [BIN, "serve", "--bg", f"http://127.0.0.1:{int(port)}"]
    print(f"+ {' '.join(command)}")
    result = run(command)
    if result.returncode != 0:
        raise PublishError(
            f"`{' '.join(command)}` failed: {(result.stderr or result.stdout).strip()[:200]}"
        )
    ran: list[Sequence[str]] = [[BIN, "serve", "status", "--json"], list(command)]

    status = run([BIN, "serve", "status"])
    ran.append([BIN, "serve", "status"])
    url = _first_url(status.stdout)
    if url:
        print(f"published → {url}  (tailnet auth is the way in)")
    else:
        print(f"published — `{BIN} serve status` names the URL")
    return Published(url=url, command=command, ran=ran)


def unpublish(
    port: int,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _default_run,
) -> bool:
    """Take the route back down. A dangling route points at a dead port."""
    if which(BIN) is None:
        return False
    print(f"+ {BIN} serve --https=443 off")
    result = run([BIN, "serve", "--https=443", "off"])
    if result.returncode != 0:
        log.warning(
            "could not take the published route down: %s",
            (result.stderr or result.stdout).strip()[:160],
        )
        return False
    return True


def _funnel_enabled(status_json: str) -> bool:
    """Whether the node has Funnel on, from `tailscale serve status --json`.

    The JSON's `Funnel` map is non-empty exactly when something is funnelled.
    An unreadable shape is treated as *no* — the publish command itself is
    `serve`, which is tailnet-only whatever else the node is doing.
    """
    try:
        raw = json.loads(status_json or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(raw, dict):
        return False
    funnel = raw.get("Funnel")
    return isinstance(funnel, dict) and bool(funnel)


def _first_url(text: str) -> str:
    """The tailnet URL `tailscale serve status` prints, if it prints one."""
    for token in str(text or "").split():
        if token.startswith("https://") and token.rstrip("."):
            return token.rstrip(".,)")
    return ""
