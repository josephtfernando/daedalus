"""Resources: declarative external access + the encrypted credential store."""

from .registry import (
    OPENCODE,
    OPENCODE_SCOPES,
    Resource,
    ResourceRegistry,
    provision_local_cli,
)
from .secrets import SecretStore

__all__ = [
    "OPENCODE",
    "OPENCODE_SCOPES",
    "Resource",
    "ResourceRegistry",
    "SecretStore",
    "provision_local_cli",
]
