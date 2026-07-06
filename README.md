# Link Glancer

Link Glancer is a desktop application for human URL review.

It imports rows from an `xlsx` workbook into SQLite, opens review targets in a controlled browser session, records manual review results, and exports a new `xlsx` file with the final data.

## Status

- The project is under active development.
- The current architecture is task-driven.
- Windows is the primary supported platform today.
- macOS support is in progress and not yet validated as a release target.

## Core Workflow

1. Create a task from an `xlsx` source file.
2. Select a browser configuration and review settings for that task.
3. Import source rows into the application database.
4. Launch a controlled browser session and confirm the browser is ready.
5. Review URLs, record results, and continue through the task buffer.
6. Export a new `xlsx` file with the configured output fields.

## Tech Stack

- Python
- PySide6
- Playwright
- SQLite
- openpyxl

## Development

The repository uses a local virtual environment at `.venv`.

Typical setup on Windows:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[build]
python -m playwright install
python -m ruff check .
python -m compileall -q src
```

Run the app:

```powershell
python -m link_glancer.main
```

## Packaging

Windows packaging assets are included:

- `packaging/link_glancer_windows.spec`
- `scripts/build_windows.ps1`

macOS packaging assets are included for ongoing work:

- `packaging/link_glancer_macos.spec`
- `scripts/build_macos.sh`
- `docs/plans/macos-build.md`

## License

MIT
