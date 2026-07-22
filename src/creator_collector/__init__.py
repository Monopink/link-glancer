from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from creator_collector.dialog import CreatorCollectorDialog

__all__ = ["CreatorCollectorDialog"]


def __getattr__(name: str) -> object:
    if name == "CreatorCollectorDialog":
        from creator_collector.dialog import CreatorCollectorDialog

        return CreatorCollectorDialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
