"""Launcher para PM2: reenvía señales al bot y espera cierre limpio del WS."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
MAIN = ROOT / "main.py"
SHUTDOWN_TIMEOUT = 8.0

_child: subprocess.Popen[bytes] | None = None


def _stop_child() -> None:
    global _child
    if _child is None or _child.poll() is not None:
        return
    _child.terminate()
    try:
        _child.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _child.kill()
        try:
            _child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _handle(signum: int, _frame) -> None:
    _stop_child()
    raise SystemExit(0)


def main() -> int:
    global _child
    if (ROOT / "data" / "LOCAL_DISABLED").is_file():
        print("Bot local deshabilitado de forma permanente (data/LOCAL_DISABLED).", file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    _child = subprocess.Popen(
        [str(PYTHON), str(MAIN)],
        cwd=str(ROOT),
        env=env,
        creationflags=creationflags,
    )
    return _child.wait()


if __name__ == "__main__":
    sys.exit(main())
