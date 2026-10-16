from __future__ import annotations

from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.model.providers import ChatResult, ScriptedProvider, ToolCall
from daedalus.runtime import Runtime, build_runtime
from daedalus.storage import paths
from daedalus.surface.cli import ScriptedSurface


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    paths.ensure_tree(home)
    return Settings(home=home, env={}, heavy=[], cheap=None)


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    return build_runtime(settings, surface=ScriptedSurface(approve=True), provider=ScriptedProvider([]))


def tool_call(tool: str, **arguments) -> ChatResult:
    return ChatResult(tool_calls=[ToolCall(id=f"call_{tool}", name=tool, arguments=arguments)])
