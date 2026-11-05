from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    hook = args[:1] == ["hook"]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from nowitknows.codex_host import main as host_main

        result = host_main(args)
        return 0 if hook else (result or 0)
    except (Exception, SystemExit):
        if hook:
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
