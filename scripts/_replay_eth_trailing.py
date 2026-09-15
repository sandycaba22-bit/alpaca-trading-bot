"""Replay ETH paper con etapa A solo-ATR."""

from __future__ import annotations

from bot.risk.stops import StopTakeProfitPolicy

ENTRY = 2507.70
PEAK = 2513.83
TP = 2515.03
CLOSE_OLD = 2507.59
ATR = (PEAK - ENTRY) / 2.0
INITIAL_SL = ENTRY * (1.0 - 0.003)


def main() -> int:
    new = StopTakeProfitPolicy(
        stop_loss_pct=0.015,
        take_profit_pct=0.003,
        atr_trailing_mult=2.0,
        breakeven_activate_atr_mult=0.5,
        breakeven_buffer_atr_mult=0.1,
        use_breakeven_lock=True,
    )
    sl, src = new.trailing_candidate(ENTRY, 1.0, PEAK, INITIAL_SL, ATR)
    print(f"NEW sl={sl} src={src} entry={ENTRY} peak={PEAK} atr={ATR:.4f}")
    if sl is None or sl <= ENTRY:
        print("FAIL sl no supera entrada")
        return 1
    if sl <= CLOSE_OLD:
        print("FAIL sl no protege vs 2507.59")
        return 1
    if sl >= TP:
        print("FAIL sl tapa TP")
        return 1
    print(f"OK SL={sl:.4f} (+{sl - ENTRY:.4f}) src={src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
