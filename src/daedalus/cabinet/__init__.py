"""Cabinet: durable seat identities over the one loop (SPEC §7).

A seat is identity + charter + memory namespace, never a process. The charters
are files under ``$DAEDALUS_HOME/cabinet/seats/`` (see :mod:`.seat`); the loop
runs as whichever one is active — Daedalus unless the owner says otherwise.
"""

from .seat import (
    BUILTIN,
    DEFAULT_SEAT,
    builtin_charters,
    CharterError,
    Seat,
    charter_dir,
    charter_file,
    load_charters,
    pick,
    read_charter,
    write_charter,
)

__all__ = [
    "BUILTIN",
    "DEFAULT_SEAT",
    "builtin_charters",
    "CharterError",
    "Seat",
    "charter_dir",
    "charter_file",
    "load_charters",
    "pick",
    "read_charter",
    "write_charter",
]
