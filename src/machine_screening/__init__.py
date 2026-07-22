from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from machine_screening.dialog import MachineScreeningDialog

__all__ = ["MachineScreeningDialog"]


def __getattr__(name: str) -> object:
    if name == "MachineScreeningDialog":
        from machine_screening.dialog import MachineScreeningDialog

        return MachineScreeningDialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
