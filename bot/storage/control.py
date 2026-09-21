"""Pausa / reanuda el bot con un archivo local (el panel web lo escribe)."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from bot.runtime_paths import data_file


def pid_alive(pid: int | None) -> bool:
    """Comprueba si un PID existe (compatible con Windows)."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        query = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize | query, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_pid(pid: int | None) -> bool:
    if pid is None or not pid_alive(pid):
        return False
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(1, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False


class BotControl:
    def __init__(self, path: Path | None = None, pid_path: Path | None = None) -> None:
        self.path = path or data_file("control.json")
        self.pid_path = pid_path or data_file("bot.pid")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def is_paused(self) -> bool:
        return bool(self._read().get("paused", False))

    def set_paused(self, paused: bool) -> None:
        data = self._read()
        data["paused"] = bool(paused)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def write_pid(self, pid: int) -> None:
        self.pid_path.write_text(str(pid), encoding="utf-8")

    def clear_pid(self) -> None:
        if self.pid_path.exists():
            self.pid_path.unlink()

    def pid(self) -> int | None:
        if not self.pid_path.exists():
            return None
        text = self.pid_path.read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else None

    def prune_stale_pid(self) -> None:
        current = self.pid()
        if current is not None and not pid_alive(current):
            self.clear_pid()

    def is_running(self) -> bool:
        self.prune_stale_pid()
        current = self.pid()
        return pid_alive(current)

    def stop_process(self) -> bool:
        """Detiene el proceso del bot y limpia el PID."""
        current = self.pid()
        if current is None:
            return False
        stopped = kill_pid(current)
        self.clear_pid()
        return stopped

    def _read(self) -> dict:
        if not self.path.exists():
            return {"paused": False}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"paused": False}
        except (OSError, json.JSONDecodeError):
            return {"paused": False}


def control_status() -> dict:
    ctl = BotControl()
    ctl.prune_stale_pid()
    running = ctl.is_running()
    paused = ctl.is_paused()
    pid = ctl.pid() if running else None
    return {
        "paused": paused,
        "running": running,
        "active": running and not paused,
        "pid": pid,
    }
