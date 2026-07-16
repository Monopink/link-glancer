from __future__ import annotations

import argparse

from creator_collector.worker_main import run_worker
from creator_enrichment.worker_main import run_worker as run_enrichment_worker
from link_glancer.app import create_application
from link_glancer.runtime.dev import initialize_dev_mode_from_environment, set_dev_mode


def main(argv: list[str] | None = None) -> int:
    initialize_dev_mode_from_environment()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--dev-mode", action="store_true")
    parser.add_argument("--collector-worker", action="store_true")
    parser.add_argument("--creator-enrichment-worker", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    if args.dev_mode:
        set_dev_mode(True)
    if args.collector_worker:
        run_worker()
        return 0
    if args.creator_enrichment_worker:
        run_enrichment_worker()
        return 0

    app = create_application()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
