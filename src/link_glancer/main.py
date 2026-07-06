from __future__ import annotations

from link_glancer.app import create_application


def main() -> int:
    app = create_application()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
