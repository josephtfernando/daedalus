"""Cabinet: durable seat identities over the one loop (SPEC §7).

A seat is identity + charter + memory namespace, never a process. The charters
are files under ``$DAEDALUS_HOME/cabinet/seats/`` (see :mod:`.seat`); the memories
are namespaces under ``$DAEDALUS_HOME/memory/`` (see :mod:`.namespace`); and what
one seat says to another is a typed, durable message in
``$DAEDALUS_HOME/cabinet/messages.db`` (see :mod:`.protocol` and :mod:`.broker`).
The loop runs as whichever seat is active — Daedalus unless the owner says
otherwise.
"""

from .broker import Broker, Delivery, Receipt, UnknownMessage, broker_for, db_path
from .namespace import DEFAULT_NAMESPACE, NamespaceError, memory_for, namespace_for
from .protocol import (
    ACK,
    CANCEL,
    FAILURE,
    PARTIAL,
    REQUEST,
    RESULT,
    TERMINAL,
    TYPES,
    VERIFY,
    Message,
    MessageError,
    seat_key,
)
from .seat import (
    BUILTIN,
    DEFAULT_SEAT,
    CharterError,
    Seat,
    UnknownSeat,
    active_seat,
    builtin_charters,
    charter_dir,
    charter_file,
    load_charters,
    pick,
    read_charter,
    resolve,
    set_active,
    write_charter,
)
from .triage import Decision, addressed, menu, triage

__all__ = [
    "ACK",
    "BUILTIN",
    "CANCEL",
    "DEFAULT_NAMESPACE",
    "DEFAULT_SEAT",
    "FAILURE",
    "PARTIAL",
    "REQUEST",
    "RESULT",
    "TERMINAL",
    "TYPES",
    "VERIFY",
    "Broker",
    "CharterError",
    "Decision",
    "Delivery",
    "Message",
    "MessageError",
    "NamespaceError",
    "Receipt",
    "Seat",
    "UnknownMessage",
    "UnknownSeat",
    "active_seat",
    "addressed",
    "broker_for",
    "builtin_charters",
    "charter_dir",
    "charter_file",
    "db_path",
    "load_charters",
    "menu",
    "memory_for",
    "namespace_for",
    "pick",
    "read_charter",
    "resolve",
    "seat_key",
    "set_active",
    "triage",
    "write_charter",
]
