"""``python -m gnnote`` entry point; delegates to :func:`gnnote.cli.main`."""
from __future__ import annotations

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    from gnnote.cli import main as cli_main  # imported lazily: cli may be absent in tests

    result = cli_main(argv) if argv is not None else cli_main()
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
