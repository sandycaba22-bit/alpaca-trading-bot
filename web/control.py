"""Control del bot desde el panel. Respuesta JSON rapida en stdout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.storage.control import BotControl, control_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Control del bot paper")
    parser.add_argument(
        "--action",
        choices=("status", "pause", "resume", "stop"),
        required=True,
    )
    args = parser.parse_args()
    ctl = BotControl()
    cancelled = 0

    if args.action == "pause":
        ctl.set_paused(True)
    elif args.action == "resume":
        ctl.set_paused(False)
    elif args.action == "stop":
        ctl.set_paused(True)
        ctl.stop_process()

    state = control_status()
    state["cancelled_orders"] = cancelled
    json.dump(state, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
