"""Atajo: barrido vol_2x Top 50 US large caps (5Min/15Min, IS/OOS).

  python -u scripts/_sweep_vol2x_top50.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_scripts = _ROOT / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from _sweep_vol2x_universe import main  # noqa: E402

if __name__ == "__main__":
    if "--universe" not in sys.argv:
        sys.argv[1:1] = ["--universe", "top50"]
    raise SystemExit(main())
