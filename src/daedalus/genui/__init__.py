"""Generative UI — the dashboard (SPEC §3.6).

The model proposes a UI **spec**; a deterministic renderer disposes. Values are
bound **locally**, so a `local_only` value never enters a prompt.

Built so far: :mod:`~daedalus.genui.server`, the loopback server that serves the
generated artifacts and gates the control API. The spec schema, components,
theming, renderer and tuners follow — see `TICKETS.md`, Stream B.
"""

from .server import LOOPBACK, LocalServer, Response, control_token

__all__ = ["LOOPBACK", "LocalServer", "Response", "control_token"]
