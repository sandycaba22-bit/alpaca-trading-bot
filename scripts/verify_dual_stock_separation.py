#!/usr/bin/env python3
"""Comprueba que élite y Top 50 no comparten la misma cuenta Alpaca (choque paper)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.universe import (
    ELITE_TOP50_OVERLAP,
    TOP50_US_STOCK_SYMBOLS,
    apply_top50_elite_exclusion,
)


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
    elite_syms = _symbols(elite.get("SYMBOLS", ""))
    top50_raw = _symbols(top50.get("SYMBOLS", ""))
    if not top50_raw:
        top50_raw = {s.upper() for s in TOP50_US_STOCK_SYMBOLS}
        print("(Top 50 SYMBOLS vacío — asumo universo TOP50_US_STOCK_SYMBOLS)")
    exclude = top50.get("STOCK_TOP50_EXCLUDE_ELITE_SYMBOLS", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    top50_eff_list, removed = apply_top50_elite_exclusion(
        sorted(top50_raw), exclude=exclude
    )
    top50_eff = set(top50_eff_list)
    overlap_raw = elite_syms & top50_raw
    overlap_effective = elite_syms & top50_eff

    if ELITE_TOP50_OVERLAP:
        print(
            f"Solape élite∩Top50 (universo): {', '.join(sorted(ELITE_TOP50_OVERLAP))}"
        )
    if exclude and removed:
        print(f"Top 50 excluye élite ({len(removed)}): {', '.join(removed)}")

    if overlap_raw:
        print(f"Símbolos en ambos .env ({len(overlap_raw)}): {', '.join(sorted(overlap_raw))}")

    if same_key:
        if overlap_effective:
            print("")
            print(
                "CHOQUE: misma cuenta y solape efectivo tras exclusión: "
                f"{', '.join(sorted(overlap_effective))}"
            )
            print(
                "  → Pon STOCK_TOP50_EXCLUDE_ELITE_SYMBOLS=true en Top 50 "
                "o usa keys Alpaca distintas."
            )
            return 1
        if overlap_raw and not exclude:
            print("")
            print(
                "CHOQUE: misma APCA_API_KEY_ID y STOCK_TOP50_EXCLUDE_ELITE_SYMBOLS=false."
            )
            return 1
        print("")
        if overlap_raw and exclude:
            print("OK misma cuenta: Top 50 excluye tickers élite — sin solape operativo.")
        else:
            print("OK misma cuenta: SYMBOLS sin solape entre bots.")
        return 0

    print("")
    print("OK: cuentas Alpaca distintas (keys diferentes).")
    if overlap_effective:
        print(
            f"Nota: solape operativo posible ({', '.join(sorted(overlap_effective))}) "
            "— OK porque son cuentas separadas."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
