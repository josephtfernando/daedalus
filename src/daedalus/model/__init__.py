"""Model layer: one OpenAI-compatible client behind a provider chain."""

from .client import ModelClient
from .providers import ChatResult, NoProviderAvailable, ProviderError, ScriptedProvider, ToolCall
from .ratelimit import RateLimitLedger
from .routing import choose_route

__all__ = [
    "ModelClient",
    "ChatResult",
    "ToolCall",
    "ScriptedProvider",
    "ProviderError",
    "NoProviderAvailable",
    "RateLimitLedger",
    "choose_route",
]
