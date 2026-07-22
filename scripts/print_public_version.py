from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    module_path = project_root / "src" / "link_glancer" / "version_core.py"
    spec = importlib.util.spec_from_file_location("link_glancer.version_core", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load public version from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    public_version = getattr(module, "PUBLIC_VERSION", None)
    if not isinstance(public_version, str) or not public_version:
        raise SystemExit(f"invalid PUBLIC_VERSION in {module_path}")
    print(public_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
