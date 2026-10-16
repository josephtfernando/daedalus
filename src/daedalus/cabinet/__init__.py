"""Cabinet: durable seat identities over the one loop (SPEC §7).

A seat is identity + charter + memory namespace, never a process. The charters
are files under ``$DAEDALUS_HOME/cabinet/seats/`` (see :mod:`.seat`); the memories
are namespaces under ``$DAEDALUS_HOME/memory/`` (see :mod:`.namespace`). The loop
runs as whichever seat is active — Daedalus unless the owner says otherwise.
"""

from .namespace import DEFAULT_NAMESPACE, NamespaceError, memory_for, namespace_for
from .seat import (
    BUILTIN,
    DEFAULT_SEAT,
    CharterError,
    Seat,
    builtin_charters,
    charter_dir,
    charter_file,
    load_charters,
    pick,
    read_charter,
    write_charter,
)

__all__ = [
    "BUILTIN",
    "DEFAULT_NAMESPACE",
    "DEFAULT_SEAT",
    "CharterError",
    "NamespaceError",
    "Seat",
    "builtin_charters",
    "charter_dir",
    "charter_file",
    "load_charters",
    "memory_for",
    "namespace_for",
    "pick",
    "read_charter",
    "write_charter",
]
