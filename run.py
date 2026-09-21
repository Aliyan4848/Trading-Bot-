#!/usr/bin/env python3
"""Zero-install launcher.

Lets you run the bot straight from a fresh clone without `pip install -e .`:

    python run.py doctor
    python run.py backtest --config config/config.yaml
    python run.py paper --max-bars 500
    python run.py strategies

If you *have* installed the package (`pip install -e .`), the `scalper` command
does exactly the same thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scalper.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
