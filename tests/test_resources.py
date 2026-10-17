"""A local CLI is a resource, and the model is told whether it is there (T28).

`opencode` is named in SPEC §5 as a local CLI and `Resource.class_` already
carries `local_cli`, so the work is *provisioning*: the entry says what the CLI
may act in, and the model sees it before it plans. The one thing a local CLI
cannot assume is that it exists — there is no credential to prove it — so the
inventory asks `PATH`, and an absent binary is **said** rather than discovered by
a delegation failing at the end of a long task.
"""

from __future__ import annotations

from pathlib import Path

from daedalus.loop.context import build_system_prompt
from daedalus.resources.registry import (
    OPENCODE,
    OPENCODE_SCOPES,
    ResourceRegistry,
    provision_local_cli,
)
from daedalus.surface.base import InboundEvent


def _registry(tmp_path: Path) -> ResourceRegistry:
    return ResourceRegistry(tmp_path / "resources" / "registry.toml")


def test_provisioning_writes_a_local_cli_entry(tmp_path) -> None:
    registry = _registry(tmp_path)
    resource = provision_local_cli(registry, OPENCODE, scopes=OPENCODE_SCOPES)

    assert resource.class_ == "local_cli"
    assert resource.access == "local"
    assert list(resource.scopes) == list(OPENCODE_SCOPES)
    assert resource.cred_ref == "", "a local CLI has no credential to reference"

    # The file is the grant: a fresh read finds it, and it is not a comment.
    reloaded = _registry(tmp_path).get(OPENCODE)
    assert reloaded is not None
    assert reloaded.class_ == "local_cli"
    assert "workspace" in reloaded.scopes


def test_the_registry_file_is_one_a_person_can_read(tmp_path) -> None:
    registry = _registry(tmp_path)
    provision_local_cli(registry, OPENCODE, scopes=OPENCODE_SCOPES, note="the delegate CLI")

    text = registry.path.read_text(encoding="utf-8")
    assert "[[resource]]" in text
    assert 'name = "opencode"' in text
    assert 'class = "local_cli"' in text


def test_provisioning_the_same_cli_twice_does_not_duplicate_it(tmp_path) -> None:
    registry = _registry(tmp_path)
    provision_local_cli(registry, OPENCODE, scopes=OPENCODE_SCOPES)
    provision_local_cli(registry, OPENCODE, scopes=("narrower",))

    assert len(registry.all()) == 1
    # The existing grant is not overwritten: a re-run of the wizard must not widen
    # scopes the owner may have narrowed by hand.
    assert registry.get(OPENCODE).scopes == list(OPENCODE_SCOPES)


def test_inventory_says_when_a_local_cli_is_missing(tmp_path) -> None:
    registry = _registry(tmp_path)
    provision_local_cli(registry, "opencode-absent", scopes=("workspace",))

    listed = registry.inventory(resolver=lambda name: None)
    assert "opencode-absent" in listed
    assert "NOT INSTALLED" in listed, listed
    assert "installed at" not in listed


def test_inventory_says_where_a_local_cli_is(tmp_path) -> None:
    registry = _registry(tmp_path)
    provision_local_cli(registry, OPENCODE, scopes=OPENCODE_SCOPES)

    listed = registry.inventory(resolver=lambda name: "/usr/bin/opencode")
    assert "installed at /usr/bin/opencode" in listed, listed
    assert "scopes=workspace,net" in listed, listed


def test_the_model_sees_a_provisioned_cli_at_startup(runtime) -> None:
    provision_local_cli(runtime.resources, OPENCODE, scopes=OPENCODE_SCOPES)

    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))

    assert "opencode" in prompt
    assert "workspace,net" in prompt


def test_the_model_is_told_when_the_cli_is_absent(runtime) -> None:
    """The absence is in the prompt, not discovered by failing halfway through.

    A name nothing provides stands in for "not installed" so the assertion does
    not depend on whether the machine running the tests has `opencode`.
    """
    provision_local_cli(runtime.resources, "daedalus-not-a-real-cli", scopes=("workspace",))

    prompt = build_system_prompt(runtime, InboundEvent(text="hi", user_id="owner"))

    assert "daedalus-not-a-real-cli" in prompt
    assert "NOT INSTALLED" in prompt


def test_an_absent_cli_never_stops_a_startup(tmp_path) -> None:
    """A hand-written `added_at` is a note, not an identity: unreadable is not fatal."""
    path = tmp_path / "resources" / "registry.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[[resource]]\n"
        'name = "odd"\n'
        'class = "local_cli"\n'
        'access = "local"\n'
        "scopes = []\n"
        'added_at = "not a date"\n',
        encoding="utf-8",
    )

    registry = ResourceRegistry(path)  # must not raise

    assert registry.get("odd") is not None
    assert registry.get("odd").added_at > 0
