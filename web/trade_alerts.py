"""JSON de alertas de operaciones (todos los bots) para el panel."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.notify.trade_alert_feed import list_alerts


def main() -> int:
    after = 0
    limit = 80
    for arg in sys.argv[1:]:
        if arg.startswith("--after="):
            after = int(arg.split("=", 1)[1] or 0)
        elif arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1] or 80)
    payload = {"alerts": list_alerts(after_id=after, limit=min(limit, 200))}
    json.dump(payload, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
