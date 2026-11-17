"""The workspace guard: blocked paths, allowed roots, and a refusal the tool reports.

Personal trees, credentials, and shell history are out of bounds by shape, not by
the model remembering; paths under the declared home resolve, and the files tool
turns the guard's refusal into a sentence rather than a traceback.
"""

from __future__ import annotations

import pytest

from daedalus.config import Settings
from daedalus.tools import guard


def test_the_guard_refuses_blocked_paths_and_allows_paths_under_home(
    settings: Settings,
) -> None:
    # outside the workspace
    with pytest.raises(guard.OutOfBounds):
        guard.resolve("/etc/passwd", settings)

    # a secret inside the home is still a secret
    with pytest.raises(guard.OutOfBounds):
        guard.resolve(str(settings.home / ".env"), settings)

    # and a personal tree elsewhere on the disk
    with pytest.raises(guard.OutOfBounds):
        guard.resolve("~/.ssh/id_rsa", settings)

    # ordinary files under the declared home resolve
    resolved = guard.resolve(str(settings.home / "notes" / "todo.md"), settings)
    assert str(resolved).startswith(str(settings.home))


async def test_the_files_tool_reports_the_refusal(runtime) -> None:
    from daedalus.tools.files import FilesTool

    result = await FilesTool().run({"op": "read", "path": "/etc/shadow"}, runtime)

    assert not result.ok
    assert "outside the permitted workspace" in result.output
