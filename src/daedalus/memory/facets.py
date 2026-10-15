"""The facets modelled for the owner (SPEC §5)."""

from __future__ import annotations

FACETS: tuple[str, ...] = (
    "competitive_programming",
    "aptitude_dsa",
    "body",
    "income_target",
    "application_pipeline",
    "projects",
    "research",
    "published_posts",
    "uncategorized",
)


def normalize_facet(raw: str | None) -> str:
    if not raw:
        return "uncategorized"
    facet = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return facet or "uncategorized"
