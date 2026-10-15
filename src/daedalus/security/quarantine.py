"""Quarantine: anything fetched becomes ``untrusted_source`` on return.

A quarantined value may be read and summarized, but acting on it requires the
owner or a deterministic derivation step. This module is the single place that
performs that downgrade, so the rule is auditable.
"""

from __future__ import annotations

from .provenance import DataClass, Labeled, Origin


class Quarantine:
    """Ingest external content and hand back labelled values."""

    def ingest(
        self,
        text: str,
        *,
        source_ref: str,
        data_class: DataClass = DataClass.PUBLIC,
    ) -> Labeled:
        """Admit external content. Always ``untrusted_source``."""
        return Labeled(
            value=text,
            origin=Origin.UNTRUSTED_SOURCE,
            data_class=data_class,
            source_ref=source_ref,
        )

    def derive(self, text: str, *, from_: Labeled, source_ref: str | None = None) -> Labeled:
        """Produce a local derivation of quarantined content."""
        return Labeled(
            value=text,
            origin=Origin.LOCAL_DERIVED,
            data_class=from_.data_class,
            source_ref=source_ref or from_.source_ref,
        )
