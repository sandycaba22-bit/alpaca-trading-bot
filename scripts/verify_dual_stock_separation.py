#!/usr/bin/env python3
"""Comprueba que élite y Top 50 no comparten la misma cuenta Alpaca (choque paper)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _symbols(raw: str) -> set[str]:
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def main() -> int:
    elite_path = ROOT / ".env.stocks"
    top50_path = ROOT / ".env.stocks_top50"
    elite = _load_env(elite_path)
    top50 = _load_env(top50_path)

    missing = []
    if not elite_path.is_file():
        missing.append(str(elite_path))
    if not top50_path.is_file():
        missing.append(str(top50_path))
    if missing:
        print("FALTA:", ", ".join(missing), file=sys.stderr)
        return 2

    key_e = elite.get("APCA_API_KEY_ID", "")
    key_t = top50.get("APCA_API_KEY_ID", "")
    base_e = elite.get("APCA_API_BASE_URL", "")
    base_t = top50.get("APCA_API_BASE_URL", "")

    print("=== Dual acciones (paper / live) ===")
    print(f"élite key prefix: {key_e[:4]}…" if len(key_e) >= 4 else "élite key: (vacía)")
    print(f"Top50 key prefix: {key_t[:4]}…" if len(key_t) >= 4 else "Top50 key: (vacía)")
    print(f"élite URL: {base_e or '(vacía)'}")
    print(f"Top50 URL: {base_t or '(vacía)'}")

    if not key_e or not key_t:
        print("ERROR: falta APCA_API_KEY_ID en algún .env", file=sys.stderr)
        return 2

    same_key = key_e == key_t
    overlap = _symbols(elite.get("SYMBOLS", "")) & _symbols(top50.get("SYMBOLS", ""))

    if overlap:
        print(f"Símbolos en ambos universos ({len(overlap)}): {', '.join(sorted(overlap))}")
        if same_key:
            print(
                "  → Con la MISMA cuenta, dos bots pueden comprar el mismo ticker el mismo día."
            )
        else:
            print("  → OK si las cuentas Alpaca son distintas (exposición separada).")

    if same_key:
        print("")
        print("CHOQUE: misma APCA_API_KEY_ID en .env.stocks y .env.stocks_top50.")
        print("Solución: segunda paper en Alpaca + keys distintas. Ver deploy/PAPER_DUAL_ACCOUNTS.md")
        return 1

    print("")
    print("OK: cuentas Alpaca distintas (keys diferentes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
