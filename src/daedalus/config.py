"""Settings — layered configuration.

Precedence (highest first):

1. environment variables (``$DAEDALUS_HOME/.env`` is parsed into ``Settings.env``, and
   consulted from there — it is *not* written into ``os.environ``, so it is not
   inherited by commands the agent runs)
2. ``$DAEDALUS_HOME/config.toml``
3. built-in defaults below

Nothing here reads the code repo. State lives in ``$DAEDALUS_HOME`` (ARCHITECTURE §5).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .storage import paths

#: The port the dashboard binds unless the owner moves it. It lives here, not in
#: the server, because it is an owner setting first and a server default second:
#: `tailscale serve` installs a route to a *number*, so this number is part of the
#: owner's configuration, not an implementation detail of one process.
DEFAULT_DASHBOARD_PORT = 8787

# --- default provider chain (SPEC §4) -------------------------------------

NIM_BASE = "https://integrate.api.nvidia.com/v1"
ZAI_BASE = "https://api.z.ai/api/paas/v4"
LOCAL_BASE = "http://127.0.0.1:11434/v1"

#: Where the local SearXNG listens if the owner runs one. Override with
#: ``DAEDALUS_SEARCH_URL``; set it to ``off`` to disable the backend entirely.
DEFAULT_SEARCH_URL = "http://127.0.0.1:8888"

DEFAULT_HEAVY: list[dict[str, Any]] = [
    {
        "name": "nim-step-5-preview",
        "base_url": NIM_BASE,
        "model": "stepfun-ai/step-5-preview",
        "api_key_env": "NVIDIA_API_KEY",
        "timeout_s": 60,
        "context_window": 131072,
    },
    {
        "name": "nim-glm-5.3-flash",
        "base_url": NIM_BASE,
        "model": "z-ai/glm-5.3-flash",
        "api_key_env": "NVIDIA_API_KEY",
        "timeout_s": 60,
        "context_window": 131072,
    },
    {
        "name": "zai-glm-4.7-flash",
        "base_url": ZAI_BASE,
        "model": "glm-4.7-flash",
        "api_key_env": "ZAI_API_KEY",
        "timeout_s": 60,
        "context_window": 131072,
    },
    {
        "name": "zai-glm-4.5-flash",
        "base_url": ZAI_BASE,
        "model": "glm-4.5-flash",
        "api_key_env": "ZAI_API_KEY",
        "timeout_s": 60,
        "context_window": 131072,
    },
]

#: There is **no default cheap model**, by design. A local model is a thing the
#: owner pulls, so a default that names one is a default that 404s on every
#: routing question — which is exactly what happened here: `bonsai-8b` was
#: written into the config by the template, never pulled, and every triage call
#: spent itself on a 404 before falling back. Absent a cheap route, routing goes
#: to the heavy chain: slower, and never wrong about a model that is not there.
DEFAULT_CHEAP: dict[str, Any] | None = None


# --- .env -----------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` .env. Never logs values."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# --- config.toml ----------------------------------------------------------


def default_config_toml() -> str:
    lines = [
        "# Daedalus configuration. Written by scripts/setup.sh; edit by hand freely.",
        "",
        "[owner]",
        "discord_users = []          # only these users may DM the agent",
        "",
        "[models]",
        "# A local model answers the routing question (which seat takes this). It",
        "# is optional, and there is no default: a name written here that is not",
        "# pulled is a 404 on every message. Set it only to a model you have —",
        "# e.g. cheap = { name = \"qwen3.5-4b-local\", base_url = \"http://127.0.0.1:11434/v1\",",
        "# model = \"qwen3.5-4b-abliterated\", api_key_env = \"\", timeout_s = 120,",
        "# retries = 0, context_window = 32768 } — and routing goes to the heavy",
        "# chain when it is absent.",
        "heavy = [",
    ]
    for p in DEFAULT_HEAVY:
        lines.append(f"  {_toml_inline(p)},")
    lines += [
        "]",
        "# An entry may also carry retries = 2 (extra tries on a 429/5xx or a",
        "# connection blip), retry_budget_s = 10.0 (how long those tries may",
        "# spend in total before the chain moves on), deadline_s = 120.0 (the",
        "# whole call, retries included; timeout_s bounds silence, not duration),",
        "# cooldown_s = 300.0 (how long a key is benched after it says it is out",
        "# of quota) and context_window = 131072 (the model's window, so the",
        "# request can be trimmed before the provider has to refuse it). Omitted",
        "# values take those defaults; retries = 0 turns retrying off for that",
        "# entry, and context_window = 0 means the window is unknown.",
        "",
        "[approvals]",
        'default_tier = "auto"      # auto | ask | block  (self-contained actions)',
        'egress_tier = "ask"        # anything leaving the machine',
        "",
        "[loop]",
        "max_steps = 16             # tool-call round trips per turn",
        "",
        "[surface]",
        'mode = "discord"           # discord | cli',
        "",
        "[ui]",
        "port = 8787                # fixed: the tailnet route points at this number",
        "publish = true             # behind the tailnet, never the public internet",
        "",
        "[proactive]",
        "enabled = true             # may the agent speak first, unprompted?",
        'quiet_start = "22:30"      # no wakes inside quiet hours; a capability alert still fires',
        'quiet_end = "07:30"',
        "max_per_day = 4            # the agent may also stay silent; this caps how often it speaks",
    ]
    return "\n".join(lines) + "\n"


def _toml_inline(d: dict[str, Any]) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, str):
            parts.append(f'{k} = "{v}"')
        else:
            parts.append(f"{k} = {v}")
    return "{ " + ", ".join(parts) + " }"


# --- settings -------------------------------------------------------------


@dataclass(slots=True)
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key_env: str = ""
    window_s: int = 60
    max_calls: int = 40
    timeout_s: int = 180
    #: Extra tries for a *transient* failure (429/5xx/connection blip) on this
    #: provider, before the chain moves to the next one. 0 disables retrying.
    retries: int = 2
    #: Wall-clock a provider's retries may spend in total. This is what keeps
    #: resilience from becoming latency: a 60s stall spends the budget by
    #: itself, so a stalling provider is skipped rather than retried twice.
    retry_budget_s: float = 10.0
    #: How long this entry's key is benched after it says it is out of quota.
    #: A quota window is not a pause; five minutes stops the rest of the turn
    #: from paying the same tries at every step. The provider's own
    #: ``Retry-After`` wins when it is longer, capped at an hour.
    cooldown_s: float = 300.0
    #: The total a *single call* may take, retries included. ``timeout_s``
    #: bounds silence; this bounds duration, so a provider that trickles (or a
    #: retry chain) cannot hold the turn for minutes. 0 disables the bound.
    deadline_s: float = 120.0
    #: The model's context window, in tokens. 0 means unknown. When known, the
    #: engine estimates the request (chars/4) and trims old tool results
    #: *before* the call, so overflow recovery stays an emergency path.
    context_window: int = 0

    def api_key(self, env: dict[str, str]) -> str:
        if not self.api_key_env:
            return ""
        return env.get(self.api_key_env, "") or os.environ.get(self.api_key_env, "")


@dataclass(slots=True)
class Settings:
    home: Path
    env: dict[str, str]
    raw: dict[str, Any] = field(default_factory=dict)
    heavy: list[ProviderConfig] = field(default_factory=list)
    cheap: ProviderConfig | None = None
    owner_users: set[str] = field(default_factory=set)
    default_tier: str = "auto"
    egress_tier: str = "ask"
    max_steps: int = 16
    surface_mode: str = "discord"
    # Proactive speech (SPEC §9). The agent may speak first; these bound it, and
    # the bounds are enforced in code (scheduler/proactive.py), not by the model.
    proactive_enabled: bool = True
    quiet_start: str = "22:30"
    quiet_end: str = "07:30"
    proactive_max_per_day: int = 4
    #: The port the dashboard binds, and the reason it is not ephemeral: the
    #: tailnet route `tailscale serve` installs points at a *number*, so a port
    #: chosen per run leaves the route pointing at the last run's port. Fixed
    #: means a restart republishes the same route, and a second instance cannot
    #: silently take the first one's place — the bind fails instead.
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    #: Whether the daemon puts the dashboard behind the tailnet (SPEC §3.5).
    #: Tailnet-only either way: this is *whether*, never *how public*.
    publish_dashboard: bool = True

    # -- lookups ----------------------------------------------------------

    def secret(self, key: str) -> str:
        return self.env.get(key, "") or os.environ.get(key, "")

    @property
    def discord_token(self) -> str:
        return self.secret("DISCORD_BOT_TOKEN")

    @property
    def search_url(self) -> str:
        """Where the local SearXNG listens. Empty or ``off`` disables it.

        SearXNG is a service the owner runs and therefore a *search backend that
        answers in JSON* — nothing depends on another site's HTML staying still,
        which is what took `search` out on 17 Oct. It is tried first, DuckDuckGo
        is the fallback, and both are optional, so an owner who runs neither
        loses nothing but a refused connection on loopback.
        """
        raw = self.secret("DAEDALUS_SEARCH_URL").strip()
        if raw.lower() in ("off", "none"):
            return ""
        return raw or DEFAULT_SEARCH_URL

    def config_path(self) -> Path:
        return self.home / "config.toml"

    def env_path(self) -> Path:
        return self.home / ".env"


def _providers(entries: list[dict[str, Any]]) -> list[ProviderConfig]:
    out: list[ProviderConfig] = []
    for e in entries:
        out.append(
            ProviderConfig(
                name=str(e.get("name", "unnamed")),
                base_url=str(e.get("base_url", "")),
                model=str(e.get("model", "")),
                api_key_env=str(e.get("api_key_env", "")),
                window_s=int(e.get("window_s", 60)),
                max_calls=int(e.get("max_calls", 40)),
                timeout_s=int(e.get("timeout_s", 180)),
                retries=int(e.get("retries", 2)),
                retry_budget_s=float(e.get("retry_budget_s", 10.0)),
                cooldown_s=float(e.get("cooldown_s", 300.0)),
                deadline_s=float(e.get("deadline_s", 120.0)),
                context_window=int(e.get("context_window", 0)),
            )
        )
    return out


def load_settings(home: Path | None = None) -> Settings:
    home = home or paths.home()
    env = load_env(home / ".env")
    raw: dict[str, Any] = {}
    cfg = home / "config.toml"
    if cfg.exists():
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))

    owner = raw.get("owner", {})
    models = raw.get("models", {})
    approvals = raw.get("approvals", {})
    loop = raw.get("loop", {})
    surface = raw.get("surface", {})
    proactive = raw.get("proactive", {})
    ui = raw.get("ui", {})

    # Servable roots (T34/T35): owner-owned data in its own file, so a folder
    # can be added by editing one file and the dashboard's editor writes THIS
    # file rather than config.toml — whose comments would not survive a TOML
    # round-trip. Its presence wins for the `files.roots` key; its absence
    # leaves whatever config.toml declared.
    roots_path = home / "roots.toml"
    if roots_path.exists():
        try:
            table = tomllib.loads(roots_path.read_text(encoding="utf-8"))
            files = dict(raw.get("files", {}) or {})
            files["roots"] = [str(p) for p in (table.get("roots") or [])]
            raw["files"] = files
        except (tomllib.TOMLDecodeError, OSError):
            raw.setdefault("files", {}).setdefault("roots", [])
    else:
        raw.setdefault("files", {"roots": list((raw.get("files", {}) or {}).get("roots", []))})

    users = owner.get("discord_users", []) or []
    # env wins over config for the allowlist
    env_users = env.get("DISCORD_ALLOWED_USERS") or os.environ.get("DISCORD_ALLOWED_USERS", "")
    if env_users:
        users = [u.strip() for u in env_users.split(",") if u.strip()]

    heavy = _providers(models.get("heavy") or DEFAULT_HEAVY)
    cheap_entry = models.get("cheap") or DEFAULT_CHEAP
    cheap = _providers([cheap_entry])[0] if cheap_entry else None

    return Settings(
        home=home,
        env=env,
        raw=raw,
        heavy=heavy,
        cheap=cheap,
        owner_users={str(u) for u in users},
        default_tier=str(approvals.get("default_tier", "auto")),
        egress_tier=str(approvals.get("egress_tier", "ask")),
        max_steps=int(loop.get("max_steps", 16)),
        surface_mode=str(surface.get("mode", "discord")),
        proactive_enabled=bool(proactive.get("enabled", True)),
        quiet_start=str(proactive.get("quiet_start", "22:30")),
        quiet_end=str(proactive.get("quiet_end", "07:30")),
        proactive_max_per_day=int(proactive.get("max_per_day", 4)),
        dashboard_port=int(ui.get("port", DEFAULT_DASHBOARD_PORT)),
        publish_dashboard=bool(ui.get("publish", True)),
    )
