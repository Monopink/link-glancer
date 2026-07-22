from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creator_enrichment.dialog import CreatorEnrichmentDialog

__all__ = ["CreatorEnrichmentDialog"]


def __getattr__(name: str) -> object:
    if name == "CreatorEnrichmentDialog":
        from creator_enrichment.dialog import CreatorEnrichmentDialog

        return CreatorEnrichmentDialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
