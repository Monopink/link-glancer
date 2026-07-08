from __future__ import annotations

import argparse

from creator_collector.worker_main import run_worker
from link_glancer.app import create_application


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--collector-worker", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    if args.collector_worker:
        run_worker()
        return 0

    app = create_application()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
