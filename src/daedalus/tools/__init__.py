"""Primitives (Layer 1) and the registry builder."""

from .base import Tool, ToolResult
from .registry import ToolRegistry

__all__ = ["Tool", "ToolResult", "ToolRegistry", "build_registry"]


def build_registry() -> ToolRegistry:
    """Register the primitives. Adding a primitive must not touch the loop."""
    from .ask import AskTool
    from .browser import BrowserTool
    from .calendar import CalendarTool
    from .capability import CapabilityTool
    from .code import CodeTool
    from .delegate import DelegateTool
    from .discord import DiscordTool
    from .files import FilesTool
    from .http import HttpTool
    from .memory import MemoryTool
    from .message import MessageTool
    from .process import ProcessTool
    from .publish import PushTool
    from .remind import RemindTool
    from .schedule import ScheduleTool
    from .search import SearchTool
    from .secrets import SecretsTool
    from .shell import ShellTool

    registry = ToolRegistry()
    for tool in (
        FilesTool(),
        ShellTool(),
        CodeTool(),
        HttpTool(),
        SearchTool(),
        BrowserTool(),
        ProcessTool(),
        MemoryTool(),
        MessageTool(),
        CalendarTool(),
        ScheduleTool(),
        RemindTool(),
        DiscordTool(),
        AskTool(),
        SecretsTool(),
        CapabilityTool(),
        DelegateTool(),
        PushTool(),
    ):
        registry.register(tool)
    return registry
