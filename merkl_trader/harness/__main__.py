"""``python -m merkl_trader.harness --config trader.toml``."""

from __future__ import annotations

import sys

from merkl_trader.harness.loop import main

if __name__ == "__main__":
    sys.exit(main())
