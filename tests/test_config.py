"""Provider-chain configuration invariants (SPEC §4)."""

from __future__ import annotations

from pathlib import Path

from daedalus.config import load_settings
from daedalus.storage import paths


def _settings(tmp_path: Path):
    home = tmp_path / "home"
    paths.ensure_tree(home)
    return load_settings(home)


def test_every_default_provider_has_a_stall_ceiling(tmp_path: Path) -> None:
    """Each entry bounds *silence*, not answer length (SPEC §4).

    The client streams, so a ceiling never cuts a long answer off — but a
    provider with no ceiling at all can stall an entire turn. The cheap local
    route is a provider too: it must carry the same ceiling.
    """
    settings = _settings(tmp_path)
    assert settings.heavy, "the default chain is empty"
    for provider in settings.heavy:
        assert provider.timeout_s == 60, provider.name
    assert settings.cheap is not None
    assert settings.cheap.timeout_s == 60


def test_default_chain_is_fastest_first(tmp_path: Path) -> None:
    """Order is a measured decision, not a preference (SPEC §4)."""
    settings = _settings(tmp_path)
    assert [p.model for p in settings.heavy] == [
        "stepfun-ai/step-5-preview",
        "z-ai/glm-5.3-flash",
        "glm-4.7-flash",
        "glm-4.5-flash",
    ]


def test_the_local_route_needs_no_credential(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.cheap is not None
    assert settings.cheap.api_key_env == ""
