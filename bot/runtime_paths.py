"""Directorio de datos configurable por perfil (stocks / crypto / hybrid)."""

from __future__ import annotations

from pathlib import Path

from bot.config import PROJECT_ROOT

_DATA_DIR: Path = PROJECT_ROOT / "data"


def configure_runtime_paths(settings) -> Path:
    """Fija DATA_DIR según settings; debe llamarse tras load_settings()."""
    global _DATA_DIR
    _DATA_DIR = settings.data_dir
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


def get_data_dir() -> Path:
    return _DATA_DIR


def data_file(name: str) -> Path:
    return get_data_dir() / name
