"""Launcher PM2 crypto-night (sin tocar run_bot.py stocks)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "main_crypto_night.py"
SHUTDOWN_TIMEOUT = 15.0

_child: subprocess.Popen[bytes] | None = None


def _venv_python() -> Path:
    win = ROOT / ".venv" / "Scripts" / "python.exe"
    nix = ROOT / ".venv" / "bin" / "python"
    if win.is_file():
        return win
    if nix.is_file():
        return nix
    return Path(sys.executable)


def _stop_child() -> None:
    global _child
    if _child is None or _child.poll() is not None:
        return
    _child.terminate()
    try:
        _child.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        _child.kill()


def _handle(signum: int, _frame) -> None:
    _stop_child()
    raise SystemExit(0)


def main() -> int:
    global _child
    if (ROOT / "data" / "LOCAL_DISABLED").is_file():
        print("Runtime local deshabilitado (data/LOCAL_DISABLED).", file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    _child = subprocess.Popen(
        [str(_venv_python()), str(MAIN)],
        cwd=str(ROOT),
        env=env,
        creationflags=creationflags,
    )
    return _child.wait()


if __name__ == "__main__":
    sys.exit(main())
