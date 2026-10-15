"""Resources: declarative external access + the encrypted credential store."""

from .registry import Resource, ResourceRegistry
from .secrets import SecretStore

__all__ = ["Resource", "ResourceRegistry", "SecretStore"]
