from __future__ import annotations

from pathlib import Path

from link_glancer.tasks.importer import (
    import_task_workbook,
    read_workbook_headers,
    read_workbook_sheet_names,
    workbook_headers_exist,
)
from link_glancer.tasks.models import TaskSnapshot


class WorkbookImporter:
    def list_sheet_names(self, workbook_path: Path) -> list[str]:
        return read_workbook_sheet_names(workbook_path)

    def list_headers(self, workbook_path: Path, *, sheet_name: str, header_row: int) -> list[str]:
        return read_workbook_headers(workbook_path, sheet_name=sheet_name, header_row=header_row)

    def check_headers(
        self, workbook_path: Path, *, sheet_name: str, header_row: int, headers: list[str]
    ) -> tuple[list[str], list[str]]:
        return workbook_headers_exist(
            workbook_path, sheet_name=sheet_name, header_row=header_row, headers=headers
        )

    def import_rows(
        self,
        workbook_path: Path,
        task_snapshot: TaskSnapshot,
    ) -> list[tuple[int, dict[str, object]]]:
        return import_task_workbook(workbook_path, task_snapshot)
