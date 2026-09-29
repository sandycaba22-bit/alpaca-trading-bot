"""Wrapper — ver strategies/crypto_night/sweeps/README.md"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.crypto_night.sweeps.run_mapping_sweep import main

if __name__ == "__main__":
    raise SystemExit(main())
