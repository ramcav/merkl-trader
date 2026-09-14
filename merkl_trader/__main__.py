"""``python -m merkl_trader --config trader.toml``."""

from __future__ import annotations

import sys

from merkl_trader.trader import main

if __name__ == "__main__":
    sys.exit(main())
