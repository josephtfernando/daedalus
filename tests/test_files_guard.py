from __future__ import annotations

import pytest

from daedalus.config import Settings
from daedalus.tools import guard


def test_refuses_paths_outside_the_workspace(settings: Settings) -> None:
    with pytest.raises(guard.OutOfBounds):
        guard.resolve("/etc/passwd", settings)


def test_refuses_dotenv_even_inside_home(settings: Settings) -> None:
    with pytest.raises(guard.OutOfBounds):
        guard.resolve(str(settings.home / ".env"), settings)


def test_refuses_ssh_keys(settings: Settings) -> None:
    with pytest.raises(guard.OutOfBounds):
        guard.resolve("~/.ssh/id_rsa", settings)


def test_allows_paths_under_home(settings: Settings) -> None:
    resolved = guard.resolve(str(settings.home / "notes" / "todo.md"), settings)
    assert str(resolved).startswith(str(settings.home))


async def test_files_tool_reports_the_refusal(runtime) -> None:
    from daedalus.tools.files import FilesTool

    result = await FilesTool().run({"op": "read", "path": "/etc/shadow"}, runtime)
    assert not result.ok
    assert "outside the permitted workspace" in result.output
