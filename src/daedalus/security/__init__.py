"""Security spine: provenance labels, sink policy, quarantine."""

from .provenance import DataClass, Labeled, Origin
from .quarantine import Quarantine
from .sink import may_authorize, may_egress

__all__ = ["DataClass", "Labeled", "Origin", "Quarantine", "may_authorize", "may_egress"]
