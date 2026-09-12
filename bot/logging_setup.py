"""Logging profesional: consola, archivo, redaccion de secretos y auditoria."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.security.secrets import RedactingFilter

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
AUDIT_FORMAT = "%(asctime)s | AUDIT | %(message)s"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


class _PnLColorFormatter(logging.Formatter):
    """Colorea [VERDE] / [ROJO] solo en consola."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return (
            message.replace("[VERDE]", f"{GREEN}[VERDE]{RESET}")
            .replace("[ROJO]", f"{RED}[ROJO]{RESET}")
            .replace("[NEUTRO]", f"{YELLOW}[NEUTRO]{RESET}")
        )


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    log_dir = log_dir or Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    redactor = RedactingFilter()
    if not any(isinstance(item, RedactingFilter) for item in root.filters):
        root.addFilter(redactor)

    if not root.handlers:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        try:
            from colorama import just_fix_windows_console

            just_fix_windows_console()
        except Exception:
            pass

        console = logging.StreamHandler(sys.stdout)
        console.addFilter(redactor)
        console.setFormatter(_PnLColorFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(console)

        file_handler = RotatingFileHandler(
            log_dir / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.addFilter(redactor)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(file_handler)

    _setup_audit_logger(log_dir, redactor)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("alpaca").setLevel(logging.WARNING)
    logging.getLogger("alpaca.data.live").setLevel(logging.INFO)


def _setup_audit_logger(log_dir: Path, redactor: RedactingFilter) -> None:
    audit_log = logging.getLogger("bot.security.audit")
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    if audit_log.handlers:
        return
    handler = RotatingFileHandler(
        log_dir / "security_audit.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    handler.addFilter(redactor)
    handler.setFormatter(logging.Formatter(AUDIT_FORMAT, datefmt=DATE_FORMAT))
    audit_log.addHandler(handler)
