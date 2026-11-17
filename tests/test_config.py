"""Provider-chain configuration invariants (SPEC §4)."""

from __future__ import annotations

from pathlib import Path

from daedalus.config import load_settings
from daedalus.storage import paths


def _settings(tmp_path: Path):
    home = tmp_path / "home"
    paths.ensure_tree(home)
    return load_settings(home)


def test_the_default_chain_is_fastest_first_with_a_stall_ceiling_on_every_entry(
    tmp_path: Path,
) -> None:
    """Order is a measured decision, and each entry bounds *silence* (SPEC §4).

    The client streams, so a ceiling never cuts a long answer off — but a
    provider with no ceiling at all can stall an entire turn. And there is no
    default cheap model: a local route names a model the owner pulled, so a
    template that invents one 404s on every routing question (which is what
    `bonsai-8b` did here). Absent one, routing goes to the heavy chain.
    """
    settings = _settings(tmp_path)

    assert settings.heavy, "the default chain is empty"
    for provider in settings.heavy:
        assert provider.timeout_s == 60, provider.name
    assert [p.model for p in settings.heavy] == [
        "stepfun-ai/step-5-preview",
        "z-ai/glm-5.3-flash",
        "glm-4.7-flash",
        "glm-4.5-flash",
    ]

    assert settings.cheap is None, "the default config names no local model"

    # …and a cheap route the owner *does* name is a provider with the same
    # stall ceiling, needing no credential.
    (tmp_path / "home" / "config.toml").write_text(
        '[models]\ncheap = { name = "local", base_url = "http://127.0.0.1:11434/v1", '
        'model = "pulled-model", api_key_env = "", timeout_s = 120 }\n',
        encoding="utf-8",
    )
    named = load_settings(tmp_path / "home").cheap
    assert named is not None and named.timeout_s == 120 and named.api_key_env == ""


def test_the_roots_list_is_the_owners_to_edit_and_starts_empty(tmp_path: Path) -> None:
    """T35/T34: the servable roots are owner-owned data, file-first."""
    # case: without roots.toml the list starts empty — nothing is served by default.
    home = tmp_path / "home"
    paths.ensure_tree(home)
    assert load_settings(home).raw["files"]["roots"] == []

    # case: roots.toml wins over the defaults.
    (home / "roots.toml").write_text('roots = ["/srv/web"]\n', encoding="utf-8")
    assert load_settings(home).raw["files"]["roots"] == ["/srv/web"]
