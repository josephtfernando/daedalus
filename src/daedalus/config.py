"""Settings — layered configuration.

Precedence (highest first):

1. environment variables (``$DAEDALUS_HOME/.env`` is loaded into ``os.environ``)
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

# --- default provider chain (SPEC §4) -------------------------------------

NIM_BASE = "https://integrate.api.nvidia.com/v1"
ZAI_BASE = "https://api.z.ai/api/paas/v4"
LOCAL_BASE = "http://127.0.0.1:11434/v1"

DEFAULT_HEAVY: list[dict[str, Any]] = [
    {
        "name": "nim-step-5-preview",
        "base_url": NIM_BASE,
        "model": "stepfun-ai/step-5-preview",
        "api_key_env": "NVIDIA_API_KEY",
        "timeout_s": 60,
    },
    {
        "name": "nim-glm-5.3-flash",
        "base_url": NIM_BASE,
        "model": "z-ai/glm-5.3-flash",
        "api_key_env": "NVIDIA_API_KEY",
        "timeout_s": 60,
    },
    {
        "name": "zai-glm-4.7-flash",
        "base_url": ZAI_BASE,
        "model": "glm-4.7-flash",
        "api_key_env": "ZAI_API_KEY",
        "timeout_s": 60,
    },
    {
        "name": "zai-glm-4.5-flash",
        "base_url": ZAI_BASE,
        "model": "glm-4.5-flash",
        "api_key_env": "ZAI_API_KEY",
        "timeout_s": 60,
    },
]

DEFAULT_CHEAP: dict[str, Any] = {
    "name": "bonsai-8b-local",
    "base_url": LOCAL_BASE,
    "model": "bonsai-8b",
    "api_key_env": "",  # local runner needs no key
}


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
        f"cheap = {_toml_inline(DEFAULT_CHEAP)}",
        "heavy = [",
    ]
    for p in DEFAULT_HEAVY:
        lines.append(f"  {_toml_inline(p)},")
    lines += [
        "]",
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

    # -- lookups ----------------------------------------------------------

    def secret(self, key: str) -> str:
        return self.env.get(key, "") or os.environ.get(key, "")

    @property
    def discord_token(self) -> str:
        return self.secret("DISCORD_BOT_TOKEN")

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

    users = owner.get("discord_users", []) or []
    # env wins over config for the allowlist
    env_users = env.get("DISCORD_ALLOWED_USERS") or os.environ.get("DISCORD_ALLOWED_USERS", "")
    if env_users:
        users = [u.strip() for u in env_users.split(",") if u.strip()]

    heavy = _providers(models.get("heavy") or DEFAULT_HEAVY)
    cheap_entry = models.get("cheap") or DEFAULT_CHEAP
    cheap = _providers([cheap_entry])[0]

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
    )
